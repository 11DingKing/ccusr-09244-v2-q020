"""异常监测持久化服务集成测试。

覆盖：窗口边界、迟到数据只重算受影响的未确认区间、低样本、
重复计算幂等、阈值附近状态抑制、确认锁定、规则变更不覆盖旧事件、
重启恢复，以及确认/忽略/恢复的操作者与依据留痕。
"""

from datetime import datetime, timedelta, timezone

from app.models import (
    Annotation,
    AnomalyEvent,
    AnomalyEventAction,
    AnomalyRule,
    AnomalyRuleRevision,
    AnomalyWindowResult,
    OperationData,
    RobotModel,
    Scene,
    Skill,
)
from app.services import anomaly_monitor as monitor

UTC = timezone.utc
T0 = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def make_dimensions(db):
    model = RobotModel(name="RM-T", manufacturer="M")
    scene = Scene(name="场景T", category="测试")
    skill = Skill(name="技能T", category="测试")
    db.add_all([model, scene, skill])
    db.flush()
    return model.id, scene.id, skill.id


def add_operation(db, ids, business_time, is_success, *, ingestion_at=None):
    """写入一条已标注作业；ingestion_at 模拟数据实际到达/补录的时刻。"""
    model_id, scene_id, skill_id = ids
    arrived = _naive(ingestion_at or business_time)
    op = OperationData(
        robot_model_id=model_id,
        scene_id=scene_id,
        skill_id=skill_id,
        motion_trajectory={"waypoints": [1]},
        perception_records={"camera": 1},
        timestamp_start=_naive(business_time),
        timestamp_end=_naive(business_time + timedelta(minutes=1)),
        created_at=arrived,
    )
    db.add(op)
    db.flush()
    db.add(Annotation(
        operation_data_id=op.id,
        is_success=is_success,
        failure_category=None if is_success else "其他",
        annotation_time=arrived,
        created_at=arrived,
    ))
    db.commit()
    return op


def fill_window(db, ids, window_start, failures, successes, *, ingestion_at=None):
    for i in range(failures):
        add_operation(db, ids, window_start + timedelta(minutes=2 + i), False, ingestion_at=ingestion_at)
    for i in range(successes):
        add_operation(db, ids, window_start + timedelta(minutes=20 + i), True, ingestion_at=ingestion_at)


def make_rule(db, ids, start_at=T0, **overrides):
    fields = dict(
        name="失败率监测",
        robot_model_id=ids[0],
        scene_id=ids[1],
        skill_id=ids[2],
        window_minutes=60,
        step_minutes=60,
        minimum_sample_count=5,
        trigger_threshold=0.5,
        recovery_threshold=0.2,
        trigger_consecutive_windows=1,
        recovery_consecutive_windows=1,
    )
    fields.update(overrides)
    return monitor.create_rule(db, operator="运营-甲", start_at=start_at, **fields)


def open_events(db, rule):
    return db.query(AnomalyEvent).filter(
        AnomalyEvent.rule_id == rule.id,
        AnomalyEvent.status == monitor.STATUS_OPEN,
    ).all()


# ---------- 触发、恢复、幂等 ----------

def test_trigger_explains_sample_range_and_rate(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=1)  # 0.8 失败率

    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    assert report.windows_total == 1
    assert len(report.triggered) == 1

    event = report.triggered[0]
    assert event.status == "open"
    assert event.trigger_total == 5
    assert event.trigger_failures == 4
    assert event.trigger_failure_rate == 0.8
    # 查询模型展示触发计算所用的样本范围
    assert event.trigger_sample_from == _naive(T0)
    assert event.trigger_sample_to == _naive(T0 + timedelta(hours=1))
    action = event.actions[0]
    assert action.action == "triggered" and action.operator == "system"
    assert action.detail["sample_from"].startswith("2025-06-01T00:00:00")


def test_repeated_evaluation_is_idempotent(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=1)
    as_of = T0 + timedelta(hours=1, minutes=1)

    first = monitor.evaluate_rule(db, rule, as_of=as_of)
    db.expire_all()
    second = monitor.evaluate_rule(db, rule, as_of=as_of)

    assert len(first.triggered) == 1
    assert second.triggered == [] and second.recovered == [] and second.retracted == []
    # 没有新数据：未确认窗口也不重算
    assert second.windows_recomputed == 0
    assert db.query(AnomalyEvent).filter(AnomalyEvent.rule_id == rule.id).count() == 1
    assert db.query(AnomalyEventAction).count() == 1
    window = db.query(AnomalyWindowResult).filter(AnomalyWindowResult.rule_id == rule.id).one()
    assert (window.total, window.failures) == (5, 4)


def test_auto_recovery_after_consecutive_recovery_windows(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=1)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))

    fill_window(db, ids, T0 + timedelta(hours=1), failures=1, successes=9)  # 0.1
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    assert len(report.recovered) == 1
    event = db.query(AnomalyEvent).one()
    assert event.status == "recovered"
    assert {a.action for a in event.actions} == {"triggered", "auto_recovered"}
    assert event.actions[-1].operator == "system"


# ---------- 迟到数据 ----------

def test_late_record_only_recomputes_affected_unconfirmed_window(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=1, successes=4)                       # 0.2 正常
    fill_window(db, ids, T0 + timedelta(hours=1), failures=1, successes=4) # 0.2 正常
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    assert open_events(db, rule) == []

    # 迟到 5 条失败补录进第一个窗口（0.6 失败率），第二个窗口不受影响
    late_at = T0 + timedelta(hours=3)
    fill_window(db, ids, T0, failures=5, successes=0, ingestion_at=late_at)

    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=3, minutes=1))
    assert report.windows_recomputed == 1  # 只有第一个窗口重算
    assert len(report.triggered) == 1
    event = report.triggered[0]
    assert event.first_trigger_window_start == _naive(T0)

    rows = {
        r.window_start: r for r in
        db.query(AnomalyWindowResult).filter(AnomalyWindowResult.rule_id == rule.id).all()
    }
    first = rows[_naive(T0)]
    second = rows[_naive(T0 + timedelta(hours=1))]
    assert (first.total, first.failures, first.signal) == (10, 6, "breach")
    assert (second.total, second.failures) == (5, 1)  # 未被重算波及


def test_confirmed_windows_are_locked_against_late_data(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=1)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    event = open_events(db, rule)[0]

    # 人工确认并锁定第一窗口
    monitor.confirm_event(
        db, event, operator="运营-乙", reason="现场确认真实异常",
        through_window_start=T0,
    )
    window = db.query(AnomalyWindowResult).filter(AnomalyWindowResult.rule_id == rule.id).one()
    assert window.confirmed is True and window.confirmed_by == "运营-乙"

    # 迟到多条成功记录本应把失败率拉到阈值以下，但窗口已冻结
    fill_window(db, ids, T0, failures=0, successes=10,
                ingestion_at=T0 + timedelta(hours=4))
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=4, minutes=1))
    assert report.windows_recomputed == 0
    db.refresh(window)
    db.refresh(event)
    assert (window.total, window.failures) == (5, 4)
    assert event.status == "open"  # 未被迟到数据改写
    actions = [a.action for a in event.actions]
    assert "confirm" in actions
    confirm = next(a for a in event.actions if a.action == "confirm")
    assert confirm.operator == "运营-乙" and confirm.reason == "现场确认真实异常"


def test_late_data_retracts_unconfirmed_event_when_basis_disappears(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=1)  # 0.8 触发
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    assert len(open_events(db, rule)) == 1

    # 迟到补录 10 条成功样本：失败率降到 4/15，不再越限
    fill_window(db, ids, T0, failures=0, successes=10,
                ingestion_at=T0 + timedelta(hours=2))
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    assert len(report.retracted) == 1
    event = db.query(AnomalyEvent).one()
    assert event.status == "retracted"
    assert any(a.action == "retracted" for a in event.actions)
    # 撤回事件本身保留，不删除历史
    assert event.trigger_failure_rate == 0.8


# ---------- 低样本与抑制 ----------

def test_low_sample_window_does_not_trigger(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=0)  # 100% 失败但只有 4 条
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    assert report.triggered == []
    row = db.query(AnomalyWindowResult).one()
    assert row.total == 4 and row.signal == "inconclusive"


def test_threshold_flapping_does_not_toggle(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    # 窗口交替 0.5 越限 / 0.4 迟滞带内：第一次越限触发后，
    # 迟滞带内波动既不会关闭异常，后续越限也不会另开新事件。
    pattern = [(5, 5), (4, 6)]
    for hour, (fails, succ) in enumerate(pattern * 4):
        fill_window(db, ids, T0 + timedelta(hours=hour), fails, succ)
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=8, minutes=1))
    assert len(report.triggered) == 1
    assert report.recovered == []
    assert db.query(AnomalyEvent).filter(AnomalyEvent.rule_id == rule.id).count() == 1

    # 只有真正连续回落到恢复阈值才关闭这同一条异常
    fill_window(db, ids, T0 + timedelta(hours=8), failures=1, successes=9)
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=9, minutes=1))
    assert len(report.recovered) == 1
    assert db.query(AnomalyEvent).filter(
        AnomalyEvent.rule_id == rule.id,
        AnomalyEvent.status == "recovered",
    ).count() == 1


# ---------- 规则变更 ----------

def test_rule_revision_change_keeps_old_events_and_windows(db):
    ids = make_dimensions(db)
    base = T0
    rule = make_rule(db, ids, start_at=base, trigger_threshold=0.5)
    fill_window(db, ids, base, failures=4, successes=1)
    monitor.evaluate_rule(db, rule, as_of=base + timedelta(hours=1, minutes=1))
    event = open_events(db, rule)[0]
    assert event.rule_revision == 1
    assert event.rule_snapshot["trigger_threshold"] == 0.5

    # 调严阈值到 0.3：新版本生效
    monitor.update_rule(
        db, rule, {"trigger_threshold": 0.3, "change_reason": "运营周会决议调严"},
        operator="运营-丙",
    )
    db.refresh(rule)
    assert rule.revision == 2
    revisions = db.query(AnomalyRuleRevision).order_by(AnomalyRuleRevision.revision).all()
    assert [r.revision for r in revisions] == [1, 2]
    assert revisions[1].change_reason == "运营周会决议调严"

    monitor.evaluate_rule(db, rule)
    db.refresh(event)
    # 旧事件仍挂在版本 1 上，参数快照不变；异常在旧版本区间未恢复，
    # 由新版本接替时统一收尾为 superseded（保留证据和操作流水）。
    assert event.rule_revision == 1
    assert event.status == "superseded"
    assert event.rule_snapshot["trigger_threshold"] == 0.5
    assert any(a.action == "superseded" and a.operator == "system" for a in event.actions)
    # 旧版本窗口统计保留
    old_windows = db.query(AnomalyWindowResult).filter(
        AnomalyWindowResult.rule_id == rule.id,
        AnomalyWindowResult.rule_revision == 1,
    ).all()
    assert [(w.total, w.failures) for w in old_windows] == [(5, 4)]


# ---------- 人工处置 ----------

def test_manual_ignore_and_recover_keep_operator_and_reason(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=5, successes=0)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    event = open_events(db, rule)[0]

    monitor.ignore_event(db, event, operator="值班-丁", reason="已知计划性停机")
    db.refresh(event)
    assert event.status == "ignored"

    # 忽略后的事件不再被自动评估改动，即使出现恢复窗口
    fill_window(db, ids, T0 + timedelta(hours=1), failures=0, successes=5)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    db.refresh(event)
    assert event.status == "ignored"
    actions = [(a.action, a.operator, a.reason) for a in event.actions]
    assert ("ignore", "值班-丁", "已知计划性停机") in actions


def test_manual_recover_records_basis(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=5, successes=0)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    event = open_events(db, rule)[0]

    monitor.recover_event(db, event, operator="运营-乙", reason="现场处置完成，设备复机")
    db.refresh(event)
    assert event.status == "recovered"
    action = next(a for a in event.actions if a.action == "recover")
    assert action.operator == "运营-乙"
    assert action.detail["latest_failure_rate"] == 1.0


# ---------- 重启恢复 ----------

def test_restart_with_new_session_recovers_state(db_engine):
    _, SessionLocal = db_engine
    db = SessionLocal()
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    fill_window(db, ids, T0, failures=4, successes=1)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    rule_id = rule.id
    db.close()

    # 模拟进程重启：换一个全新会话继续评估
    db2 = SessionLocal()
    rule2 = db2.get(AnomalyRule, rule_id)
    report = monitor.evaluate_rule(db2, rule2, as_of=T0 + timedelta(hours=2, minutes=1))
    assert report.triggered == []  # 状态从库中恢复，不重复触发
    events = db2.query(AnomalyEvent).filter(AnomalyEvent.rule_id == rule.id).all()
    assert len(events) == 1
    assert events[0].status == "open"
    assert db2.query(AnomalyEventAction).count() == 1
    db2.close()


# ---------- 窗口边界 ----------

def test_sample_exactly_on_window_boundary(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids)
    # 各放 5 条；边界样本 T0+1h 必须计入第二窗
    for i in range(5):
        add_operation(db, ids, T0 + timedelta(minutes=10 * i), i % 2 == 0)
    add_operation(db, ids, T0 + timedelta(hours=1), False)
    for i in range(4):
        add_operation(db, ids, T0 + timedelta(hours=1, minutes=10 * (i + 1)), True)

    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    rows = {
        r.window_start: r for r in
        db.query(AnomalyWindowResult).filter(AnomalyWindowResult.rule_id == rule.id).all()
    }
    assert rows[_naive(T0)].total == 5
    assert rows[_naive(T0 + timedelta(hours=1))].total == 5


def test_revisions_evaluate_independently(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids, start_at=T0)

    # 版本 1：触发后在同一版本区间恢复
    fill_window(db, ids, T0, failures=4, successes=1)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    fill_window(db, ids, T0 + timedelta(hours=1), failures=1, successes=9)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    old_event = db.query(AnomalyEvent).one()
    assert old_event.status == "recovered"

    # 版本 2 从 T0+3h 生效
    monitor.update_rule(
        db, rule, {"trigger_threshold": 0.3},
        operator="运营-丙", start_at=T0 + timedelta(hours=3),
    )
    db.refresh(rule)
    assert rule.revision == 2

    # 评估到 T0+3h：已恢复的旧事件不被版本接替改动
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=3, minutes=1))
    db.refresh(old_event)
    assert old_event.status == "recovered"

    # 新版本窗口里出现高失败率：按新版本独立触发一条新事件
    fill_window(db, ids, T0 + timedelta(hours=3), failures=4, successes=1)
    report = monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=4, minutes=1))
    assert len(report.triggered) == 1
    new_event = report.triggered[0]
    assert new_event.rule_revision == 2
    assert new_event.rule_snapshot["trigger_threshold"] == 0.3
    events = db.query(AnomalyEvent).order_by(AnomalyEvent.id).all()
    assert [e.rule_revision for e in events] == [1, 2]
    assert [e.status for e in events] == ["recovered", "open"]
    # 窗口统计按版本分开存储
    revisions_present = {
        (r.rule_revision, r.window_start)
        for r in db.query(AnomalyWindowResult).all()
    }
    assert (1, _naive(T0)) in revisions_present
    assert (2, _naive(T0 + timedelta(hours=3))) in revisions_present


def test_old_open_event_superseded_on_revision_change(db):
    ids = make_dimensions(db)
    rule = make_rule(db, ids, start_at=T0)
    fill_window(db, ids, T0, failures=5, successes=0)
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=1, minutes=1))
    event = open_events(db, rule)[0]

    monitor.update_rule(db, rule, {"trigger_threshold": 0.3}, operator="运营-丙",
                        start_at=T0 + timedelta(hours=2))
    monitor.evaluate_rule(db, rule, as_of=T0 + timedelta(hours=2, minutes=1))
    db.refresh(event)
    assert event.status == "superseded"
    assert event.rule_snapshot["trigger_threshold"] == 0.5  # 证据快照不被覆盖
