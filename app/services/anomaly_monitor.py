"""失败率异常监测的持久化服务。

评估方式是“重放”：每次评估按作业业务时间生成连续窗口，未确认窗口用最新
样本重新计算（迟到/补录数据由此生效），已确认窗口直接跳过；随后把窗口
序列交给纯领域状态机（:mod:`app.services.anomaly`）重放出异常片段，再与
库中事件对账。因此：

- 重复执行评估是幂等的，不会产生重复事件或重复流水；
- 状态全部保存在 SQLite 中，进程重启后再次评估结果一致；
- 规则参数变更会生成新版本，新旧版本各自独立评估，旧事件保留在旧版本
  上，既不被覆盖也不会被新参数自动改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.models import (
    Annotation,
    AnomalyEvent,
    AnomalyEventAction,
    AnomalyRule,
    AnomalyRuleRevision,
    AnomalyWindowResult,
    OperationData,
)
from app.services.anomaly import (
    AnomalyConfigError,
    AnomalyRuleConfig,
    SampleRow,
    WindowSample,
    align_grid_start,
    bucket_samples,
    simulate,
    window_starts,
)

SYSTEM_OPERATOR = "system"

# 事件状态
STATUS_OPEN = "open"            # 异常持续中，等待处理
STATUS_IGNORED = "ignored"      # 人工判定忽略，自动评估不再改动
STATUS_RECOVERED = "recovered"  # 已恢复（自动或人工）
STATUS_RETRACTED = "retracted"  # 未确认前被迟到数据修正，触发依据消失
STATUS_SUPERSEDED = "superseded"  # 规则口径变更，旧版本事件随旧版本结束

TERMINAL_STATUSES = {STATUS_IGNORED, STATUS_RECOVERED, STATUS_RETRACTED, STATUS_SUPERSEDED}
MANUAL_ACTIONS = {"confirm", "ignore", "recover"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """SQLite 读回的时间通常是 naive 的，按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def naive_utc(value: datetime) -> datetime:
    """SQLite 文本比较需要 naive UTC（列内格式统一即可）。"""
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _config(fields: dict[str, Any]) -> AnomalyRuleConfig:
    return AnomalyRuleConfig(
        window=timedelta(minutes=fields["window_minutes"]),
        step=timedelta(minutes=fields["step_minutes"]),
        minimum_sample_count=fields["minimum_sample_count"],
        trigger_threshold=fields["trigger_threshold"],
        recovery_threshold=fields["recovery_threshold"],
        trigger_consecutive_windows=fields.get("trigger_consecutive_windows", 1),
        recovery_consecutive_windows=fields.get("recovery_consecutive_windows", 1),
    )


def config_from_rule(rule: AnomalyRule) -> AnomalyRuleConfig:
    return _config(
        {
            "window_minutes": rule.window_minutes,
            "step_minutes": rule.step_minutes,
            "minimum_sample_count": rule.minimum_sample_count,
            "trigger_threshold": rule.trigger_threshold,
            "recovery_threshold": rule.recovery_threshold,
            "trigger_consecutive_windows": rule.trigger_consecutive_windows,
            "recovery_consecutive_windows": rule.recovery_consecutive_windows,
        }
    )


def snapshot_from_rule(rule: AnomalyRule) -> dict:
    return {
        "name": rule.name,
        "robot_model_id": rule.robot_model_id,
        "scene_id": rule.scene_id,
        "skill_id": rule.skill_id,
        "window_minutes": rule.window_minutes,
        "step_minutes": rule.step_minutes,
        "minimum_sample_count": rule.minimum_sample_count,
        "trigger_threshold": rule.trigger_threshold,
        "recovery_threshold": rule.recovery_threshold,
        "trigger_consecutive_windows": rule.trigger_consecutive_windows,
        "recovery_consecutive_windows": rule.recovery_consecutive_windows,
    }


def validate_rule_payload(values: dict[str, Any]) -> AnomalyRuleConfig:
    config = _config(values)
    config.validate()
    return config


# 修改这些字段意味着判定口径变化，需要生成新版本
SCOPED_PARAM_FIELDS = {
    "robot_model_id", "scene_id", "skill_id",
    "window_minutes", "step_minutes", "minimum_sample_count",
    "trigger_threshold", "recovery_threshold",
    "trigger_consecutive_windows", "recovery_consecutive_windows",
}


def create_rule(db: Session, *, operator: str | None, start_at: datetime | None = None, **fields) -> AnomalyRule:
    change_reason = fields.pop("change_reason", None)
    validate_rule_payload(fields)
    start = ensure_utc(start_at) or utc_now()
    rule = AnomalyRule(
        created_by=operator,
        revision=1,
        revision_start_at=naive_utc(start),
        change_reason=change_reason,
        **fields,
    )
    db.add(rule)
    db.flush()
    db.add(AnomalyRuleRevision(
        rule_id=rule.id,
        revision=1,
        snapshot=snapshot_from_rule(rule),
        started_at=naive_utc(start),
        created_by=operator,
        change_reason=change_reason,
    ))
    db.commit()
    db.refresh(rule)
    return rule


def update_rule(db: Session, rule: AnomalyRule, changes: dict, *, operator: str | None, start_at: datetime | None = None) -> AnomalyRule:
    """更新规则。口径字段变化时生成新版本：旧版本窗口与旧事件保持原样。"""
    if not changes:
        return rule
    changes = dict(changes)
    merged = {
        "window_minutes": rule.window_minutes,
        "step_minutes": rule.step_minutes,
        "minimum_sample_count": rule.minimum_sample_count,
        "trigger_threshold": rule.trigger_threshold,
        "recovery_threshold": rule.recovery_threshold,
        "trigger_consecutive_windows": rule.trigger_consecutive_windows,
        "recovery_consecutive_windows": rule.recovery_consecutive_windows,
        **changes,
    }
    validate_rule_payload(merged)

    scoped_change = any(field in SCOPED_PARAM_FIELDS for field in changes)
    for field_name, value in changes.items():
        setattr(rule, field_name, value)

    if scoped_change:
        rule.revision += 1
        rule.revision_start_at = naive_utc(ensure_utc(start_at) or utc_now())
        db.flush()
        db.add(AnomalyRuleRevision(
            rule_id=rule.id,
            revision=rule.revision,
            snapshot=snapshot_from_rule(rule),
            started_at=rule.revision_start_at,
            created_by=operator,
            change_reason=changes.get("change_reason"),
        ))
    db.commit()
    db.refresh(rule)
    return rule


def _scope_query(query, scope: dict):
    if scope.get("robot_model_id") is not None:
        query = query.filter(OperationData.robot_model_id == scope["robot_model_id"])
    if scope.get("scene_id") is not None:
        query = query.filter(OperationData.scene_id == scope["scene_id"])
    if scope.get("skill_id") is not None:
        query = query.filter(OperationData.skill_id == scope["skill_id"])
    return query


def _load_enriched_samples(db: Session, scope: dict, start: datetime, end: datetime) -> list[tuple[SampleRow, datetime]]:
    """读取范围内已标注作业样本，额外返回每条样本的数据摄入时间。

    摄入时间取作业创建、标注创建、标注更新三者的最大值，用于水位比对，
    把迟到/补录数据触发的重算限定在受影响窗口。

    SQLite 对带时区偏移的时间戳只按文本存储，直接与 naive UTC 比较不可靠，
    因此先按机型/场景/技能范围取出，再在 Python 侧规范化时区后过滤。
    """
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    rows = (
        _scope_query(
            db.query(
                OperationData.timestamp_start,
                Annotation.is_success,
                OperationData.created_at,
                Annotation.created_at,
                Annotation.updated_at,
            ),
            scope,
        )
        .join(Annotation, Annotation.operation_data_id == OperationData.id)
        .all()
    )
    enriched: list[tuple[SampleRow, datetime]] = []
    for happened_at, is_success, op_created, ann_created, ann_updated in rows:
        moment = ensure_utc(happened_at)
        if moment is None or not (start_utc <= moment < end_utc):
            continue
        ingestion_candidates = [ensure_utc(value) for value in (op_created, ann_created, ann_updated)]
        ingestion_at = max((value for value in ingestion_candidates if value is not None), default=moment)
        enriched.append((SampleRow(happened_at=moment, is_success=bool(is_success)), ingestion_at))
    return enriched


@dataclass
class EvaluationReport:
    rule_id: int
    revision: int
    as_of: datetime
    range_start: datetime
    windows_total: int = 0
    windows_recomputed: int = 0
    triggered: list[AnomalyEvent] = field(default_factory=list)
    recovered: list[AnomalyEvent] = field(default_factory=list)
    retracted: list[AnomalyEvent] = field(default_factory=list)
    superseded: list[AnomalyEvent] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "revision": self.revision,
            "as_of": self.as_of.isoformat(),
            "range_start": self.range_start.isoformat(),
            "windows_total": self.windows_total,
            "windows_recomputed": self.windows_recomputed,
            "triggered_event_ids": [event.id for event in self.triggered],
            "recovered_event_ids": [event.id for event in self.recovered],
            "retracted_event_ids": [event.id for event in self.retracted],
            "superseded_event_ids": [event.id for event in self.superseded],
        }


def _add_action(db: Session, event: AnomalyEvent, action: str, operator: str, reason: str | None, detail: dict | None = None) -> None:
    db.add(AnomalyEventAction(
        event_id=event.id,
        action=action,
        operator=operator,
        reason=reason,
        detail=detail,
    ))


def _aggregate(samples: Sequence[WindowSample]) -> tuple[int, int, float]:
    total = sum(item.total for item in samples)
    failures = sum(item.failures for item in samples)
    rate = failures / total if total else 0.0
    return total, failures, rate


def align_to_grid_floor(moment: datetime, step: timedelta) -> datetime:
    return align_grid_start(moment.astimezone(timezone.utc), step)


def _evaluate_revision(
    db: Session,
    rule: AnomalyRule,
    revision_row: AnomalyRuleRevision,
    range_end: datetime,
    moment: datetime,
    report: EvaluationReport,
    *,
    superseded: bool = False,
) -> None:
    revision = revision_row.revision
    snapshot = revision_row.snapshot
    config = _config(snapshot)
    range_start = ensure_utc(revision_row.started_at)  # type: ignore[assignment]
    if range_start is None or range_start >= range_end:
        return

    # 评估到“最新一条已标注样本所在窗口”为止：没有样本的版本区间不产出
    # 空窗口行，也避免版本切换后为整段空洞区间生成无意义窗口。
    enriched = _load_enriched_samples(db, snapshot, range_start, range_end)
    if not enriched:
        return
    last_business = max(sample.happened_at for sample, _ingestion in enriched)
    data_end = align_to_grid_floor(last_business, config.step) + config.window
    effective_end = min(range_end, data_end)

    starts = window_starts(config, range_start, effective_end)
    if not starts:
        return

    existing = {
        ensure_utc(row.window_start): row
        for row in db.query(AnomalyWindowResult).filter(
            AnomalyWindowResult.rule_id == rule.id,
            AnomalyWindowResult.rule_revision == revision,
        ).all()
    }

    # 候选窗口 = 尚未确认（或首次出现）的窗口；已确认窗口永远不进入重算集合。
    candidate_starts = [
        start for start in starts
        if start not in existing or not existing[start].confirmed
    ]
    old_watermark = ensure_utc(revision_row.samples_watermark)
    new_watermark = old_watermark

    if candidate_starts:
        sample_rows = [row for row, _ingestion in enriched]
        ingestion_times = [ingestion for _row, ingestion in enriched]
        if ingestion_times:
            new_watermark = max(
                [value for value in ingestion_times if value is not None]
                + ([old_watermark] if old_watermark else []),
            )
        buckets = {item.start: item for item in bucket_samples(sample_rows, candidate_starts, config)}

        for start in candidate_starts:
            bucket = buckets[start]
            row = existing.get(start)
            if row is None:
                # 首次出现的窗口（时间走到了新窗口）必然计算
                affected = True
            elif old_watermark is None:
                # 本版本第一次评估
                affected = True
            else:
                # 受迟到/补录影响：窗口内（按业务时间归属）存在摄入时间晚于
                # 水位的样本，或统计与已存结果不一致（覆盖标注删除等没有
                # 可靠水位信号的变更）。
                late_members = any(
                    start <= sample.happened_at < start + config.window and ingestion > old_watermark
                    for sample, ingestion in enriched
                )
                stats_changed = (
                    row.total != bucket.total
                    or row.failures != bucket.failures
                )
                affected = late_members or stats_changed
            if not affected:
                continue

            if row is None:
                row = AnomalyWindowResult(
                    rule_id=rule.id,
                    rule_revision=revision,
                    window_start=naive_utc(start),
                    window_end=naive_utc(bucket.end),
                )
                db.add(row)
            row.total = bucket.total
            row.failures = bucket.failures
            row.failure_rate = bucket.failure_rate if bucket.failure_rate is not None else None
            row.signal = bucket.signal(config)
            report.windows_recomputed += 1

    if new_watermark is not None and new_watermark != old_watermark:
        revision_row.samples_watermark = naive_utc(new_watermark)

    db.flush()

    rows = (
        db.query(AnomalyWindowResult)
        .filter(
            AnomalyWindowResult.rule_id == rule.id,
            AnomalyWindowResult.rule_revision == revision,
        )
        .order_by(AnomalyWindowResult.window_start.asc())
        .all()
    )
    start_set = set(starts)
    rows = [row for row in rows if ensure_utc(row.window_start) in start_set]
    sequence = [
        WindowSample(
            start=ensure_utc(row.window_start),  # type: ignore[arg-type]
            end=ensure_utc(row.window_end),      # type: ignore[arg-type]
            total=row.total,
            failures=row.failures,
        )
        for row in rows
    ]
    report.windows_total += len(sequence)

    episodes = simulate(sequence, config)

    stored_events = {
        ensure_utc(event.first_trigger_window_start): event
        for event in db.query(AnomalyEvent).filter(
            AnomalyEvent.rule_id == rule.id,
            AnomalyEvent.rule_revision == revision,
        ).all()
    }

    referenced: set[datetime] = set()

    for episode in episodes:
        trigger_sample = sequence[episode.trigger_index]
        trigger_start = trigger_sample.start
        referenced.add(trigger_start)
        run_from = max(0, episode.trigger_index - config.trigger_consecutive_windows + 1)
        trigger_windows = sequence[run_from: episode.trigger_index + 1]
        trigger_total, trigger_failures, trigger_rate = _aggregate(trigger_windows)
        trigger_from = trigger_windows[0].start
        trigger_to = trigger_windows[-1].end

        latest_index = episode.recovery_index if episode.recovery_index is not None else len(sequence) - 1
        latest_sample = sequence[latest_index]
        latest_rate = latest_sample.failure_rate if latest_sample.failure_rate is not None else 0.0

        event = stored_events.get(trigger_start)
        if event is None:
            event = AnomalyEvent(
                rule_id=rule.id,
                rule_revision=revision,
                rule_snapshot=snapshot,
                status=STATUS_OPEN,
                first_trigger_window_start=naive_utc(trigger_start),
                last_window_start=naive_utc(latest_sample.start),
                first_triggered_at=naive_utc(moment),
                last_change_at=naive_utc(moment),
                last_evaluated_at=naive_utc(moment),
                trigger_sample_from=naive_utc(trigger_from),
                trigger_sample_to=naive_utc(trigger_to),
                trigger_total=trigger_total,
                trigger_failures=trigger_failures,
                trigger_failure_rate=trigger_rate,
                trigger_threshold=config.trigger_threshold,
                latest_sample_from=naive_utc(latest_sample.start),
                latest_sample_to=naive_utc(latest_sample.end),
                latest_total=latest_sample.total,
                latest_failures=latest_sample.failures,
                latest_failure_rate=latest_rate,
            )
            db.add(event)
            db.flush()
            _add_action(
                db, event, "triggered", SYSTEM_OPERATOR,
                f"连续窗口失败率达到触发阈值 {config.trigger_threshold:.2%}",
                {
                    "sample_from": trigger_from.isoformat(),
                    "sample_to": trigger_to.isoformat(),
                    "total": trigger_total,
                    "failures": trigger_failures,
                    "failure_rate": trigger_rate,
                },
            )
            report.triggered.append(event)
            continue

        # 已忽略/已恢复/已撤回等终态事件保持原样，自动评估不再改动；
        # 后续再次越限会由状态机生成新触发窗口，即一条新事件。
        if event.status != STATUS_OPEN:
            continue

        event.last_evaluated_at = naive_utc(moment)
        event.latest_sample_from = naive_utc(latest_sample.start)
        event.latest_sample_to = naive_utc(latest_sample.end)
        event.latest_total = latest_sample.total
        event.latest_failures = latest_sample.failures
        event.latest_failure_rate = latest_rate
        event.last_window_start = naive_utc(latest_sample.start)

        if event.status == STATUS_OPEN and episode.recovery_index is not None:
            event.status = STATUS_RECOVERED
            event.last_change_at = naive_utc(moment)
            _add_action(
                db, event, "auto_recovered", SYSTEM_OPERATOR,
                f"连续 {config.recovery_consecutive_windows} 个窗口失败率回落到恢复阈值 "
                f"{config.recovery_threshold:.2%} 以下",
                {
                    "sample_from": latest_sample.start.isoformat(),
                    "sample_to": latest_sample.end.isoformat(),
                    "total": latest_sample.total,
                    "failures": latest_sample.failures,
                    "failure_rate": latest_rate,
                },
            )
            report.recovered.append(event)

    # 未确认、且未经任何人工操作的事件，若其触发片段在重放中消失，
    # 说明迟到/补录数据修正了触发依据：自动撤回而不是悄悄改写。
    # 旧版本被新版本接替时，这类 open 事件统一随旧版本收尾为 superseded。
    for trigger_start, event in stored_events.items():
        if trigger_start in referenced or event.status != STATUS_OPEN:
            continue
        if trigger_start not in start_set:
            continue
        if any(item.action in MANUAL_ACTIONS for item in event.actions):
            continue
        if superseded:
            event.status = STATUS_SUPERSEDED
            event.last_change_at = naive_utc(moment)
            event.last_evaluated_at = naive_utc(moment)
            _add_action(
                db, event, "superseded", SYSTEM_OPERATOR,
                "规则口径变更生成新版本，旧版本事件随旧版本结束",
            )
            report.superseded.append(event)
            continue
        event.status = STATUS_RETRACTED
        event.last_change_at = naive_utc(moment)
        event.last_evaluated_at = naive_utc(moment)
        _add_action(
            db, event, "retracted", SYSTEM_OPERATOR,
            "迟到数据重算未确认窗口后，触发窗口不再满足越限条件",
        )
        report.retracted.append(event)

    # 旧版本区间内重放仍在持续（尚未恢复）的 open 事件：
    # 新版本已从接替时刻开始另行监测，旧事件在此收尾，证据原样保留。
    if superseded:
        for trigger_start, event in stored_events.items():
            if trigger_start not in start_set or event.status != STATUS_OPEN:
                continue
            if any(item.action in MANUAL_ACTIONS for item in event.actions):
                continue
            event.status = STATUS_SUPERSEDED
            event.last_change_at = naive_utc(moment)
            event.last_evaluated_at = naive_utc(moment)
            _add_action(
                db, event, "superseded", SYSTEM_OPERATOR,
                "规则口径变更生成新版本，异常在旧版本区间内尚未恢复，随旧版本结束",
            )
            report.superseded.append(event)


def evaluate_rule(
    db: Session,
    rule: AnomalyRule,
    *,
    as_of: datetime | None = None,
    from_time: datetime | None = None,
) -> EvaluationReport:
    """按给定业务时点重放评估一个规则的所有版本。

    重复调用是幂等的：窗口统计按唯一约束更新、事件按触发窗口对账，
    不会产生重复事件或重复流水。
    """
    if not rule.is_active:
        raise AnomalyConfigError("规则已停用，不能评估")

    moment = ensure_utc(as_of) or utc_now()
    range_start = ensure_utc(from_time)
    revisions = (
        db.query(AnomalyRuleRevision)
        .filter(AnomalyRuleRevision.rule_id == rule.id)
        .order_by(AnomalyRuleRevision.revision.asc())
        .all()
    )
    if not revisions:
        raise AnomalyConfigError("规则缺少版本记录")

    report = EvaluationReport(
        rule_id=rule.id,
        revision=rule.revision,
        as_of=moment,
        range_start=ensure_utc(revisions[0].started_at) or moment,
    )

    for index, revision_row in enumerate(revisions):
        rev_start = ensure_utc(revision_row.started_at) or moment
        if range_start is not None and rev_start < range_start:
            continue
        next_start = (
            ensure_utc(revisions[index + 1].started_at)
            if index + 1 < len(revisions)
            else None
        )
        # 旧版本的区间在新版本生效时截止；当前版本评估到 moment。
        rev_end = min(next_start, moment) if next_start else moment
        if rev_start >= rev_end:
            continue
        _evaluate_revision(
            db, rule, revision_row, rev_end, moment, report,
            # 只有评估时刻已经进入新版本区间，旧版本才算被接替
            superseded=next_start is not None and moment >= next_start,
        )

    rule.last_evaluated_at = naive_utc(moment)
    db.commit()
    for event in (
        report.triggered + report.recovered
        + report.retracted + report.superseded
    ):
        db.refresh(event)
    return report


def confirm_event(
    db: Session,
    event: AnomalyEvent,
    *,
    operator: str,
    reason: str | None,
    through_window_start: datetime | None = None,
) -> AnomalyEvent:
    """确认异常，并把截至某个窗口的窗口区间锁定（迟到数据不再重算它们）。"""
    if event.status == STATUS_IGNORED:
        raise AnomalyConfigError("已忽略的事件不能确认，请先恢复")
    rule = db.query(AnomalyRule).filter(AnomalyRule.id == event.rule_id).first()
    revision_row = (
        db.query(AnomalyRuleRevision)
        .filter(
            AnomalyRuleRevision.rule_id == event.rule_id,
            AnomalyRuleRevision.revision == event.rule_revision,
        )
        .first()
    )
    config = _config(revision_row.snapshot if revision_row else snapshot_from_rule(rule))

    query = db.query(AnomalyWindowResult).filter(
        AnomalyWindowResult.rule_id == event.rule_id,
        AnomalyWindowResult.rule_revision == event.rule_revision,
        AnomalyWindowResult.confirmed.is_(False),
    )
    if through_window_start is not None:
        bound = ensure_utc(through_window_start) + config.window  # type: ignore[operator]
        query = query.filter(AnomalyWindowResult.window_end <= naive_utc(bound))
    locked = query.order_by(AnomalyWindowResult.window_start.asc()).all()
    moment = naive_utc(utc_now())
    through = None
    for row in locked:
        row.confirmed = True
        row.confirmed_by = operator
        row.confirmed_at = moment
        through = ensure_utc(row.window_end)

    event.confirmed_through = naive_utc(through) if through else event.confirmed_through
    event.last_change_at = moment
    _add_action(db, event, "confirm", operator, reason, {
        "confirmed_through": through.isoformat() if through else None,
        "locked_windows": len(locked),
    })
    db.commit()
    db.refresh(event)
    return event


def ignore_event(db: Session, event: AnomalyEvent, *, operator: str, reason: str | None) -> AnomalyEvent:
    if event.status in TERMINAL_STATUSES and event.status != STATUS_IGNORED:
        raise AnomalyConfigError(f"事件已处于终态 {event.status}，不能忽略")
    event.status = STATUS_IGNORED
    event.last_change_at = naive_utc(utc_now())
    _add_action(db, event, "ignore", operator, reason)
    db.commit()
    db.refresh(event)
    return event


def recover_event(db: Session, event: AnomalyEvent, *, operator: str, reason: str | None) -> AnomalyEvent:
    if event.status == STATUS_RECOVERED:
        raise AnomalyConfigError("事件已恢复，无需重复操作")
    event.status = STATUS_RECOVERED
    event.last_change_at = naive_utc(utc_now())
    _add_action(
        db, event, "recover", operator, reason,
        {"latest_failure_rate": event.latest_failure_rate},
    )
    db.commit()
    db.refresh(event)
    return event
