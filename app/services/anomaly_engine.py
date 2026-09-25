"""异常监测评估引擎：连续窗口计算、迟到重算与状态持久化。

评估语义：
- 窗口按作业业务时间（timestamp_start）连续切分，以纪元对齐；
- 正常推进只评估水位线之后完整闭合的窗口；
- 迟到记录（补录作业、补标注、改标注）只触发受影响且未锁定窗口的重算；
- 每个 (规则, 版本, 窗口起点) 只有一条评估记录，重复评估幂等；
- 事件状态由评估记录确定性推导，进程重启后从持久化的水位线继续。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import (
    Annotation,
    AnomalyEvent,
    AnomalyEventAction,
    AnomalyRule,
    AnomalyRuleState,
    AnomalyRuleVersion,
    AnomalyWindowEvaluation,
    OperationData,
)
from app.services.anomaly import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    EventAction,
    EventStatus,
    RuleSpec,
    WindowStat,
    align_down,
    apply_action,
    resolve_window,
    trigger_windows,
    window_locked,
    windows_between,
)


def as_utc(moment: datetime) -> datetime:
    """数据库读出的时间一律视为 UTC。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def as_naive(moment: datetime) -> datetime:
    """写入 SQLite 前统一转换为 UTC 朴素时间，避免时区偏移被静默丢弃。"""
    return as_utc(moment).replace(tzinfo=None)


def rule_spec(rule: AnomalyRule) -> RuleSpec:
    return RuleSpec(
        window_size=timedelta(minutes=rule.window_size_minutes),
        min_samples=rule.min_samples,
        trigger_threshold=rule.trigger_threshold,
        recovery_threshold=rule.recovery_threshold,
        consecutive_windows=rule.consecutive_windows,
    ).validate()


@dataclass
class EvaluationReport:
    """一次评估运行的可解释结果。"""

    rule_id: int
    rule_version: int
    watermark: datetime | None = None
    evaluated_windows: list[WindowStat] = field(default_factory=list)
    recomputed_windows: list[WindowStat] = field(default_factory=list)
    locked_windows_skipped: int = 0
    opened_event_ids: list[int] = field(default_factory=list)
    resolved_event_ids: list[int] = field(default_factory=list)


def get_or_create_state(
    db: Session,
    rule: AnomalyRule,
    spec: RuleSpec,
    now: datetime,
    start_from: datetime | None = None,
) -> AnomalyRuleState:
    """取当前版本的评估游标；首次评估时以 start_from（缺省为当前时间）对齐水位线。"""
    state = (
        db.query(AnomalyRuleState)
        .filter(AnomalyRuleState.rule_id == rule.id, AnomalyRuleState.rule_version == rule.version)
        .first()
    )
    if state is None:
        anchor = as_utc(start_from) if start_from is not None else now
        state = AnomalyRuleState(
            rule_id=rule.id,
            rule_version=rule.version,
            watermark=as_naive(align_down(anchor, spec.window_size)),
            last_run_at=as_naive(now),
        )
        db.add(state)
        db.flush()
    return state


def window_samples(db: Session, rule: AnomalyRule, start: datetime, end: datetime) -> list[tuple[int, bool]]:
    """窗口内该组合下已标注的作业记录，按业务时间落窗 [start, end)。"""
    rows = (
        db.query(OperationData.id, Annotation.is_success)
        .join(Annotation, Annotation.operation_data_id == OperationData.id)
        .filter(
            OperationData.robot_model_id == rule.robot_model_id,
            OperationData.scene_id == rule.scene_id,
            OperationData.skill_id == rule.skill_id,
            OperationData.timestamp_start >= as_naive(start),
            OperationData.timestamp_start < as_naive(end),
        )
        .order_by(OperationData.id)
        .all()
    )
    return [(int(row_id), bool(is_success)) for row_id, is_success in rows]


def upsert_evaluation(
    db: Session,
    rule: AnomalyRule,
    spec: RuleSpec,
    stat: WindowStat,
    sample_ids: list[int],
    now: datetime,
) -> tuple[AnomalyWindowEvaluation, bool]:
    """写入或更新窗口评估；样本与结果一致时幂等跳过，返回 (记录, 是否变化)。"""
    row = (
        db.query(AnomalyWindowEvaluation)
        .filter(
            AnomalyWindowEvaluation.rule_id == rule.id,
            AnomalyWindowEvaluation.rule_version == rule.version,
            AnomalyWindowEvaluation.window_start == as_naive(stat.start),
        )
        .first()
    )
    ids = [int(sid) for sid in sample_ids]
    if (
        row is not None
        and row.sample_ids == ids
        and row.sample_count == stat.sample_count
        and row.failure_count == stat.failure_count
    ):
        return row, False
    if row is None:
        row = AnomalyWindowEvaluation(
            rule_id=rule.id,
            rule_version=rule.version,
            window_start=as_naive(stat.start),
            window_end=as_naive(stat.end),
            sample_count=stat.sample_count,
            failure_count=stat.failure_count,
            failure_rate=stat.failure_rate,
            evaluable=stat.evaluable(spec),
            sample_ids=ids,
            first_computed_at=as_naive(now),
            computed_at=as_naive(now),
            recompute_count=0,
        )
        db.add(row)
        db.flush()
        return row, True
    row.sample_count = stat.sample_count
    row.failure_count = stat.failure_count
    row.failure_rate = stat.failure_rate
    row.evaluable = stat.evaluable(spec)
    row.sample_ids = ids
    row.computed_at = as_naive(now)
    row.recompute_count += 1
    db.flush()
    return row, True


def locked_intervals(db: Session, rule_id: int) -> list[tuple[datetime, datetime]]:
    """已确认、已忽略或已恢复事件覆盖的业务时间区间；这些区间的历史不再被改写。"""
    rows = (
        db.query(AnomalyEvent.locked_from, AnomalyEvent.locked_until)
        .filter(
            AnomalyEvent.rule_id == rule_id,
            AnomalyEvent.status.in_([EventStatus.ACKNOWLEDGED.value, *TERMINAL_STATUSES]),
        )
        .all()
    )
    return [(as_utc(start), as_utc(end)) for start, end in rows]


def active_event(db: Session, rule: AnomalyRule) -> AnomalyEvent | None:
    """当前版本下仍处于打开或已确认状态的事件。"""
    return (
        db.query(AnomalyEvent)
        .filter(
            AnomalyEvent.rule_id == rule.id,
            AnomalyEvent.rule_version == rule.version,
            AnomalyEvent.status.in_(ACTIVE_STATUSES),
        )
        .order_by(AnomalyEvent.id)
        .first()
    )


def find_late_windows(db: Session, rule: AnomalyRule, state: AnomalyRuleState, spec: RuleSpec) -> set[datetime]:
    """定位上次运行之后入库或变更、且业务时间落在水位线之前的记录所在窗口。

    created_at/updated_at 由 SQLite CURRENT_TIMESTAMP 生成（秒级、无小数），
    与 ORM 写入的 last_run_at 格式不同，比较前用 datetime() 归一化。
    """
    last_run = as_utc(state.last_run_at).strftime("%Y-%m-%d %H:%M:%S")
    watermark = as_utc(state.watermark)
    rows = (
        db.query(OperationData.timestamp_start)
        .outerjoin(Annotation, Annotation.operation_data_id == OperationData.id)
        .filter(
            OperationData.robot_model_id == rule.robot_model_id,
            OperationData.scene_id == rule.scene_id,
            OperationData.skill_id == rule.skill_id,
            OperationData.timestamp_start < as_naive(watermark),
            or_(
                func.datetime(OperationData.created_at) >= last_run,
                func.datetime(Annotation.created_at) >= last_run,
                func.datetime(Annotation.updated_at) >= last_run,
            ),
        )
        .all()
    )
    return {align_down(as_utc(row[0]), spec.window_size) for row in rows}


def evaluate_rule(
    db: Session,
    rule: AnomalyRule,
    until: datetime | None = None,
    now: datetime | None = None,
    start_from: datetime | None = None,
) -> EvaluationReport:
    """推进规则评估：先重算受迟到数据影响的未锁定窗口，再评估新窗口并运行状态机。"""
    current = as_utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    final = as_utc(until) if until is not None else current
    spec = rule_spec(rule)
    state = get_or_create_state(db, rule, spec, current, start_from)
    watermark = as_utc(state.watermark)
    report = EvaluationReport(rule_id=rule.id, rule_version=rule.version)
    changed = False

    # 1. 迟到记录：只重算受影响且未被确认的窗口，已锁定区间保持原样
    locked = locked_intervals(db, rule.id)
    for start in sorted(find_late_windows(db, rule, state, spec)):
        end = start + spec.window_size
        if window_locked(start, end, locked):
            report.locked_windows_skipped += 1
            continue
        samples = window_samples(db, rule, start, end)
        stat = WindowStat(start, end, len(samples), sum(1 for _, ok in samples if not ok))
        _, did_change = upsert_evaluation(db, rule, spec, stat, [sid for sid, _ in samples], current)
        if did_change:
            report.recomputed_windows.append(stat)
            changed = True

    # 2. 正常推进：评估水位线之后完整闭合的连续窗口
    for start, end in windows_between(watermark, final, spec.window_size):
        samples = window_samples(db, rule, start, end)
        stat = WindowStat(start, end, len(samples), sum(1 for _, ok in samples if not ok))
        upsert_evaluation(db, rule, spec, stat, [sid for sid, _ in samples], current)
        report.evaluated_windows.append(stat)
        changed = True
    report.watermark = report.evaluated_windows[-1].end if report.evaluated_windows else watermark
    state.watermark = as_naive(report.watermark)

    # 3. 状态机：由评估记录确定性推导，幂等且可在重启后继续
    if changed:
        run_state_machine(db, rule, spec, current, report)

    state.last_run_at = as_naive(current)
    db.commit()
    return report


def run_state_machine(db: Session, rule: AnomalyRule, spec: RuleSpec, now: datetime, report: EvaluationReport) -> None:
    rows = (
        db.query(AnomalyWindowEvaluation)
        .filter(
            AnomalyWindowEvaluation.rule_id == rule.id,
            AnomalyWindowEvaluation.rule_version == rule.version,
        )
        .order_by(AnomalyWindowEvaluation.window_start)
        .all()
    )
    stats = [
        WindowStat(as_utc(row.window_start), as_utc(row.window_end), row.sample_count, row.failure_count)
        for row in rows
    ]
    event = active_event(db, rule)

    if event is not None:
        recovery = resolve_window(stats, spec, True)
        if recovery is None:
            return
        event.status = EventStatus.RESOLVED.value
        event.resolved_at = as_naive(now)
        event.resolved_by = "system"
        event.resolve_reason = (
            f"窗口 [{recovery.start.isoformat()}, {recovery.end.isoformat()}) "
            f"失败率 {recovery.failure_rate:.4f} 回落至恢复阈值 {spec.recovery_threshold} 以下"
        )
        if recovery.end > as_utc(event.locked_until):
            event.locked_until = as_naive(recovery.end)
        db.add(
            AnomalyEventAction(
                event_id=event.id,
                action=EventAction.RESOLVE.value,
                operator="system",
                reason=event.resolve_reason,
            )
        )
        report.resolved_event_ids.append(event.id)
        return

    triggers = trigger_windows(stats, spec, False, locked_intervals(db, rule.id))
    if not triggers:
        return
    by_start = {as_utc(row.window_start): row for row in rows}
    sample_ids: list[int] = []
    total_samples = 0
    total_failures = 0
    for stat in triggers:
        row = by_start[stat.start]
        sample_ids.extend(int(sid) for sid in row.sample_ids)
        total_samples += row.sample_count
        total_failures += row.failure_count
    event = AnomalyEvent(
        rule_id=rule.id,
        rule_version=rule.version,
        status=EventStatus.OPEN.value,
        trigger_window_start=as_naive(triggers[0].start),
        trigger_window_end=as_naive(triggers[-1].end),
        trigger_failure_rate=total_failures / total_samples,
        trigger_sample_count=total_samples,
        trigger_failure_count=total_failures,
        trigger_sample_ids=sample_ids,
        threshold_snapshot=spec.snapshot(),
        locked_from=as_naive(triggers[0].start),
        locked_until=as_naive(triggers[-1].end),
        opened_at=as_naive(now),
    )
    db.add(event)
    db.flush()
    report.opened_event_ids.append(event.id)


def record_event_action(
    db: Session,
    event: AnomalyEvent,
    action: EventAction,
    operator: str,
    reason: str,
    now: datetime | None = None,
) -> AnomalyEventAction:
    """执行人工动作并留痕；非法流转抛出 AnomalyError，由调用方决定响应码。"""
    current = as_utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    target = apply_action(event.status, action)
    event.status = target.value
    if target == EventStatus.RESOLVED:
        event.resolved_at = as_naive(current)
        event.resolved_by = operator
        event.resolve_reason = reason
    record = AnomalyEventAction(event_id=event.id, action=action.value, operator=operator, reason=reason)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def snapshot_version(
    db: Session,
    rule: AnomalyRule,
    changed_by: str,
    change_reason: str,
    now: datetime | None = None,
) -> AnomalyRuleVersion:
    """保存规则当前字段的不可变版本快照。"""
    current = as_utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    snapshot = AnomalyRuleVersion(
        rule_id=rule.id,
        version=rule.version,
        name=rule.name,
        robot_model_id=rule.robot_model_id,
        scene_id=rule.scene_id,
        skill_id=rule.skill_id,
        window_size_minutes=rule.window_size_minutes,
        min_samples=rule.min_samples,
        trigger_threshold=rule.trigger_threshold,
        recovery_threshold=rule.recovery_threshold,
        consecutive_windows=rule.consecutive_windows,
        enabled=rule.enabled,
        changed_by=changed_by,
        change_reason=change_reason,
        effective_at=as_naive(current),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def apply_rule_change(
    db: Session,
    rule: AnomalyRule,
    changes: dict,
    changed_by: str,
    change_reason: str,
    now: datetime | None = None,
) -> AnomalyRuleVersion:
    """应用规则变更：校验新参数、系统恢复旧版本活动事件、递增版本并留存快照。

    旧事件及其触发快照原样保留，只追加一条系统恢复的操作记录，不被覆盖。
    """
    current = as_utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    candidate = {
        "window_size_minutes": changes.get("window_size_minutes", rule.window_size_minutes),
        "min_samples": changes.get("min_samples", rule.min_samples),
        "trigger_threshold": changes.get("trigger_threshold", rule.trigger_threshold),
        "recovery_threshold": changes.get("recovery_threshold", rule.recovery_threshold),
        "consecutive_windows": changes.get("consecutive_windows", rule.consecutive_windows),
    }
    RuleSpec(
        window_size=timedelta(minutes=candidate["window_size_minutes"]),
        min_samples=candidate["min_samples"],
        trigger_threshold=candidate["trigger_threshold"],
        recovery_threshold=candidate["recovery_threshold"],
        consecutive_windows=candidate["consecutive_windows"],
    ).validate()

    events = (
        db.query(AnomalyEvent)
        .filter(AnomalyEvent.rule_id == rule.id, AnomalyEvent.status.in_(ACTIVE_STATUSES))
        .all()
    )
    for event in events:
        event.status = EventStatus.RESOLVED.value
        event.resolved_at = as_naive(current)
        event.resolved_by = "system"
        event.resolve_reason = f"规则版本由 v{rule.version} 更替至 v{rule.version + 1}：{change_reason}"
        db.add(
            AnomalyEventAction(
                event_id=event.id,
                action=EventAction.RESOLVE.value,
                operator="system",
                reason=event.resolve_reason,
            )
        )

    for key, value in changes.items():
        setattr(rule, key, value)
    rule.version += 1
    snapshot = snapshot_version(db, rule, changed_by, change_reason, current)
    db.commit()
    db.refresh(rule)
    return snapshot
