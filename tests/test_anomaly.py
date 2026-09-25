"""异常监测的端到端测试：窗口边界、迟到数据、低样本、重复计算、状态抑制与重启恢复。"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/robot_anomaly_bootstrap.db")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.models import (
    Annotation,
    AnomalyEvent,
    AnomalyEventAction,
    AnomalyRule,
    AnomalyWindowEvaluation,
    OperationData,
    RobotModel,
    Scene,
    Skill,
)
from main import app

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
PREFIX = "/api/v1"


def make_session(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "anomaly_test.db"


@pytest.fixture()
def session(db_path):
    engine, sess = make_session(db_path)
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture()
def client(session):
    def override():
        yield session

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def resources(session):
    robot_model = RobotModel(name="RB-1000", manufacturer="ACME")
    scene = Scene(name="装配线", category="生产制造")
    skill = Skill(name="抓取", category="操作")
    session.add_all([robot_model, scene, skill])
    session.commit()
    return SimpleNamespace(robot_model_id=robot_model.id, scene_id=scene.id, skill_id=skill.id)


def create_rule(client, resources, **overrides):
    payload = {
        "name": "装配抓取失败率",
        "robot_model_id": resources.robot_model_id,
        "scene_id": resources.scene_id,
        "skill_id": resources.skill_id,
        "window_size_minutes": 60,
        "min_samples": 2,
        "trigger_threshold": 0.5,
        "recovery_threshold": 0.2,
        "consecutive_windows": 1,
        "operator": "ops-bot",
    }
    payload.update(overrides)
    resp = client.post(f"{PREFIX}/anomaly-rules", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def add_operation(session, resources, happened_at, is_success):
    operation = OperationData(
        robot_model_id=resources.robot_model_id,
        scene_id=resources.scene_id,
        skill_id=resources.skill_id,
        motion_trajectory={},
        perception_records={},
        timestamp_start=happened_at,
        timestamp_end=happened_at + timedelta(minutes=1),
    )
    session.add(operation)
    session.flush()
    session.add(
        Annotation(
            operation_data_id=operation.id,
            is_success=is_success,
            failure_category=None if is_success else "感知异常",
        )
    )
    session.commit()
    return operation


def evaluate(client, rule_id, until, start_from=None):
    payload = {"until": until.isoformat()}
    if start_from is not None:
        payload["start_from"] = start_from.isoformat()
    resp = client.post(f"{PREFIX}/anomaly-rules/{rule_id}/evaluate", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def get_event(client, event_id):
    resp = client.get(f"{PREFIX}/anomaly-events/{event_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def get_evaluations(client, rule_id):
    resp = client.get(f"{PREFIX}/anomaly-rules/{rule_id}/evaluations")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_window_boundary_and_partial_window(client, session, resources):
    rule = create_rule(client, resources, min_samples=1, trigger_threshold=0.9, recovery_threshold=0.1)
    first = add_operation(session, resources, BASE, False)
    second = add_operation(session, resources, BASE + timedelta(minutes=59, seconds=59), True)
    third = add_operation(session, resources, BASE + timedelta(hours=1), False)

    # 窗口未闭合时不评估
    report = evaluate(client, rule["id"], BASE + timedelta(minutes=30), start_from=BASE)
    assert report["evaluated_windows"] == 0
    assert get_evaluations(client, rule["id"]) == []

    # [10:00, 11:00) 闭合：边界遵循左闭右开
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1))
    assert report["evaluated_windows"] == 1
    rows = get_evaluations(client, rule["id"])
    assert len(rows) == 1
    assert rows[0]["window_start"] == "2026-01-01T10:00:00"
    assert rows[0]["window_end"] == "2026-01-01T11:00:00"
    assert rows[0]["sample_ids"] == [first.id, second.id]
    assert rows[0]["failure_count"] == 1

    # 11:00:00 的记录属于下一个窗口
    evaluate(client, rule["id"], BASE + timedelta(hours=2))
    rows = get_evaluations(client, rule["id"])
    assert len(rows) == 2
    assert rows[1]["sample_ids"] == [third.id]


def test_trigger_requires_consecutive_windows(client, session, resources):
    rule = create_rule(client, resources, min_samples=2, consecutive_windows=2)
    # 窗口1 超阈值，但连续数不足
    add_operation(session, resources, BASE + timedelta(minutes=5), False)
    add_operation(session, resources, BASE + timedelta(minutes=10), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)
    assert report["opened_event_ids"] == []

    # 窗口2 处于迟滞区（1/3 失败），连续计数中断
    for i in range(3):
        add_operation(session, resources, BASE + timedelta(hours=1, minutes=5 + i), i != 0)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=2))
    assert report["opened_event_ids"] == []

    # 窗口3 超阈值但连续数重新从 1 开始
    add_operation(session, resources, BASE + timedelta(hours=2, minutes=5), False)
    add_operation(session, resources, BASE + timedelta(hours=2, minutes=6), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=3))
    assert report["opened_event_ids"] == []

    # 窗口4 继续超阈值，窗口3-4 连续两窗触发
    add_operation(session, resources, BASE + timedelta(hours=3, minutes=5), False)
    add_operation(session, resources, BASE + timedelta(hours=3, minutes=6), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=4))
    assert len(report["opened_event_ids"]) == 1

    detail = get_event(client, report["opened_event_ids"][0])
    assert detail["event"]["status"] == "open"
    assert detail["event"]["trigger_window_start"] == "2026-01-01T12:00:00"
    assert detail["event"]["trigger_window_end"] == "2026-01-01T14:00:00"
    assert detail["event"]["trigger_sample_count"] == 4
    assert detail["event"]["trigger_failure_count"] == 4
    assert len(detail["trigger_windows"]) == 2


def test_low_sample_windows_are_not_evaluable(client, session, resources):
    rule = create_rule(client, resources, min_samples=3, consecutive_windows=1)
    # 样本量不足：不可评估，不触发
    add_operation(session, resources, BASE + timedelta(minutes=5), False)
    add_operation(session, resources, BASE + timedelta(minutes=6), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)
    assert report["opened_event_ids"] == []
    rows = get_evaluations(client, rule["id"])
    assert rows[0]["evaluable"] is False
    assert rows[0]["sample_count"] == 2

    # 补录一条后窗口变为可评估并触发
    add_operation(session, resources, BASE + timedelta(minutes=30), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1))
    assert report["recomputed_windows"] == 1
    assert len(report["opened_event_ids"]) == 1
    rows = get_evaluations(client, rule["id"])
    assert rows[0]["evaluable"] is True
    assert rows[0]["sample_count"] == 3


def test_late_data_recomputes_only_unlocked_windows(client, session, resources):
    rule = create_rule(client, resources, min_samples=1, consecutive_windows=1)
    add_operation(session, resources, BASE + timedelta(minutes=5), True)
    evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)

    # 迟到记录落入已评估窗口：只重算该窗口并触发事件
    late = add_operation(session, resources, BASE + timedelta(minutes=10), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1))
    assert report["recomputed_windows"] == 1
    assert len(report["opened_event_ids"]) == 1
    event_id = report["opened_event_ids"][0]
    rows = get_evaluations(client, rule["id"])
    assert rows[0]["sample_ids"] == sorted([rows[0]["sample_ids"][0], late.id])
    assert rows[0]["recompute_count"] == 1

    # 确认事件后窗口锁定，新的迟到记录不再改写历史
    resp = client.post(
        f"{PREFIX}/anomaly-events/{event_id}/actions",
        json={"action": "confirm", "operator": "张三", "reason": "已电话确认为真实故障"},
    )
    assert resp.status_code == 200
    add_operation(session, resources, BASE + timedelta(minutes=20), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1))
    assert report["recomputed_windows"] == 0
    assert report["locked_windows_skipped"] == 1
    rows = get_evaluations(client, rule["id"])
    assert rows[0]["sample_count"] == 2
    assert rows[0]["recompute_count"] == 1
    assert rows[0]["locked"] is True

    # 事件触发时的样本范围快照不受补录影响
    detail = get_event(client, event_id)
    assert detail["event"]["trigger_sample_count"] == 2
    assert detail["event"]["status"] == "acknowledged"


def test_late_annotation_update_recomputes_window(client, session, resources):
    rule = create_rule(client, resources, min_samples=2, consecutive_windows=1)
    first = add_operation(session, resources, BASE + timedelta(minutes=5), True)
    add_operation(session, resources, BASE + timedelta(minutes=6), True)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)
    assert report["opened_event_ids"] == []

    # 补录场景：历史标注由成功改为失败，窗口被重算并触发
    annotation = session.query(Annotation).filter(Annotation.operation_data_id == first.id).first()
    annotation.is_success = False
    annotation.failure_category = "感知异常"
    session.commit()
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1))
    assert report["recomputed_windows"] == 1
    assert len(report["opened_event_ids"]) == 1
    rows = get_evaluations(client, rule["id"])
    assert rows[0]["failure_count"] == 1
    assert rows[0]["failure_rate"] == 0.5


def test_repeated_evaluation_is_idempotent(client, session, resources):
    rule = create_rule(client, resources, min_samples=2, consecutive_windows=1)
    add_operation(session, resources, BASE + timedelta(minutes=5), False)
    add_operation(session, resources, BASE + timedelta(minutes=6), False)

    first = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)
    assert len(first["opened_event_ids"]) == 1
    for _ in range(2):
        report = evaluate(client, rule["id"], BASE + timedelta(hours=1))
        assert report["evaluated_windows"] == 0
        assert report["recomputed_windows"] == 0
        assert report["opened_event_ids"] == []

    rows = get_evaluations(client, rule["id"])
    assert len(rows) == 1
    assert rows[0]["recompute_count"] == 0
    events = client.get(f"{PREFIX}/anomaly-events", params={"rule_id": rule["id"]}).json()
    assert len(events) == 1


def test_hysteresis_prevents_flapping(client, session, resources):
    rule = create_rule(
        client, resources, min_samples=2, trigger_threshold=0.5, recovery_threshold=0.2, consecutive_windows=1
    )
    # 窗口1：失败率 1.0，触发
    add_operation(session, resources, BASE + timedelta(minutes=1), False)
    add_operation(session, resources, BASE + timedelta(minutes=2), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)
    event_id = report["opened_event_ids"][0]

    # 窗口2：失败率 0.5，仍达触发阈值但已有活动事件，不重复打开
    add_operation(session, resources, BASE + timedelta(hours=1, minutes=1), False)
    add_operation(session, resources, BASE + timedelta(hours=1, minutes=2), True)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=2))
    assert report["opened_event_ids"] == []
    assert report["resolved_event_ids"] == []

    # 窗口3：失败率 0.25，处于迟滞区，事件保持打开不恢复
    add_operation(session, resources, BASE + timedelta(hours=2, minutes=1), False)
    for i in range(3):
        add_operation(session, resources, BASE + timedelta(hours=2, minutes=10 + i), True)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=3))
    assert report["resolved_event_ids"] == []
    assert get_event(client, event_id)["event"]["status"] == "open"

    # 窗口4：失败率 0.0，回落至恢复阈值以下，系统自动恢复
    add_operation(session, resources, BASE + timedelta(hours=3, minutes=1), True)
    add_operation(session, resources, BASE + timedelta(hours=3, minutes=2), True)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=4))
    assert report["resolved_event_ids"] == [event_id]
    detail = get_event(client, event_id)
    assert detail["event"]["status"] == "resolved"
    assert detail["event"]["resolved_by"] == "system"
    assert "恢复阈值" in detail["event"]["resolve_reason"]
    assert [a["action"] for a in detail["actions"]] == ["resolve"]

    # 窗口5：再次超阈值，作为新异常打开新事件
    add_operation(session, resources, BASE + timedelta(hours=4, minutes=1), False)
    add_operation(session, resources, BASE + timedelta(hours=4, minutes=2), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=5))
    assert len(report["opened_event_ids"]) == 1
    assert report["opened_event_ids"][0] != event_id
    events = client.get(f"{PREFIX}/anomaly-events", params={"rule_id": rule["id"]}).json()
    assert len(events) == 2


def test_restart_recovers_state_from_database(client, session, resources, db_path):
    rule = create_rule(client, resources, min_samples=2, consecutive_windows=1)
    add_operation(session, resources, BASE + timedelta(minutes=1), False)
    add_operation(session, resources, BASE + timedelta(minutes=2), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)
    event_id = report["opened_event_ids"][0]

    # 模拟进程重启：关闭旧连接，用同一数据库文件建立新连接
    session.close()
    session.bind.dispose()
    engine, new_session = make_session(db_path)
    try:
        def override():
            yield new_session

        app.dependency_overrides[get_db] = override
        restarted = TestClient(app)

        # 水位线之后的恢复数据让事件自动关闭，且历史窗口不重复计算
        add_operation(new_session, resources, BASE + timedelta(hours=1, minutes=1), True)
        add_operation(new_session, resources, BASE + timedelta(hours=1, minutes=2), True)
        report = evaluate(restarted, rule["id"], BASE + timedelta(hours=2))
        assert report["evaluated_windows"] == 1
        assert report["resolved_event_ids"] == [event_id]

        rows = get_evaluations(restarted, rule["id"])
        assert len(rows) == 2
        assert all(row["recompute_count"] == 0 for row in rows)
        detail = get_event(restarted, event_id)
        assert detail["event"]["status"] == "resolved"
        assert detail["event"]["trigger_sample_count"] == 2
    finally:
        app.dependency_overrides.clear()
        new_session.close()
        engine.dispose()


def test_event_actions_keep_operator_and_reason(client, session, resources):
    rule = create_rule(client, resources, min_samples=2, consecutive_windows=1)
    add_operation(session, resources, BASE + timedelta(minutes=1), False)
    add_operation(session, resources, BASE + timedelta(minutes=2), False)
    event_id = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)["opened_event_ids"][0]

    resp = client.post(
        f"{PREFIX}/anomaly-events/{event_id}/actions",
        json={"action": "confirm", "operator": "张三", "reason": "现场确认为传感器污损"},
    )
    assert resp.status_code == 200
    assert get_event(client, event_id)["event"]["status"] == "acknowledged"

    # 已确认事件不允许重复确认
    resp = client.post(
        f"{PREFIX}/anomaly-events/{event_id}/actions",
        json={"action": "confirm", "operator": "张三", "reason": "重复操作"},
    )
    assert resp.status_code == 409

    resp = client.post(
        f"{PREFIX}/anomaly-events/{event_id}/actions",
        json={"action": "resolve", "operator": "李四", "reason": "已更换传感器并复测通过"},
    )
    assert resp.status_code == 200
    detail = get_event(client, event_id)
    assert detail["event"]["status"] == "resolved"
    assert detail["event"]["resolved_by"] == "李四"
    assert detail["event"]["resolve_reason"] == "已更换传感器并复测通过"
    assert [(a["action"], a["operator"], a["reason"]) for a in detail["actions"]] == [
        ("confirm", "张三", "现场确认为传感器污损"),
        ("resolve", "李四", "已更换传感器并复测通过"),
    ]

    # 终态事件拒绝任何后续操作
    resp = client.post(
        f"{PREFIX}/anomaly-events/{event_id}/actions",
        json={"action": "ignore", "operator": "王五", "reason": "误报"},
    )
    assert resp.status_code == 409

    # 忽略动作同样留痕
    add_operation(session, resources, BASE + timedelta(hours=1, minutes=1), False)
    add_operation(session, resources, BASE + timedelta(hours=1, minutes=2), False)
    second_id = evaluate(client, rule["id"], BASE + timedelta(hours=2))["opened_event_ids"][0]
    resp = client.post(
        f"{PREFIX}/anomaly-events/{second_id}/actions",
        json={"action": "ignore", "operator": "王五", "reason": "例行维护时段，属误报"},
    )
    assert resp.status_code == 200
    detail = get_event(client, second_id)
    assert detail["event"]["status"] == "suppressed"
    assert detail["actions"][0]["operator"] == "王五"
    assert detail["actions"][0]["reason"] == "例行维护时段，属误报"


def test_rule_change_keeps_old_events_with_original_snapshot(client, session, resources):
    rule = create_rule(client, resources, min_samples=2, trigger_threshold=0.5, consecutive_windows=1)
    add_operation(session, resources, BASE + timedelta(minutes=1), False)
    add_operation(session, resources, BASE + timedelta(minutes=2), False)
    event_id = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)["opened_event_ids"][0]

    resp = client.put(
        f"{PREFIX}/anomaly-rules/{rule['id']}",
        json={"trigger_threshold": 0.8, "changed_by": "王五", "change_reason": "误报过多，上调阈值"},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    assert resp.json()["trigger_threshold"] == 0.8

    # 旧事件被系统恢复但完整保留，阈值快照仍是触发时的 0.5
    detail = get_event(client, event_id)
    assert detail["event"]["status"] == "resolved"
    assert detail["event"]["resolved_by"] == "system"
    assert "v1" in detail["event"]["resolve_reason"]
    assert detail["event"]["threshold_snapshot"]["trigger_threshold"] == 0.5
    assert detail["actions"][-1]["operator"] == "system"

    versions = client.get(f"{PREFIX}/anomaly-rules/{rule['id']}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["trigger_threshold"] == 0.5
    assert versions[1]["trigger_threshold"] == 0.8
    assert versions[1]["changed_by"] == "王五"
    assert versions[1]["change_reason"] == "误报过多，上调阈值"

    # 新版本按新阈值评估：失败率 0.6 不再触发
    for i in range(5):
        add_operation(session, resources, BASE + timedelta(hours=1, minutes=1 + i), i < 3)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=2), start_from=BASE + timedelta(hours=1))
    assert report["opened_event_ids"] == []

    # 失败率 1.0 超过新阈值，新事件快照为 0.8
    add_operation(session, resources, BASE + timedelta(hours=2, minutes=1), False)
    add_operation(session, resources, BASE + timedelta(hours=2, minutes=2), False)
    report = evaluate(client, rule["id"], BASE + timedelta(hours=3))
    assert len(report["opened_event_ids"]) == 1
    new_event = get_event(client, report["opened_event_ids"][0])
    assert new_event["event"]["rule_version"] == 2
    assert new_event["event"]["threshold_snapshot"]["trigger_threshold"] == 0.8


def test_rule_detail_exposes_state_and_sample_scope(client, session, resources):
    rule = create_rule(client, resources, min_samples=2, consecutive_windows=1)
    first = add_operation(session, resources, BASE + timedelta(minutes=1), False)
    second = add_operation(session, resources, BASE + timedelta(minutes=2), False)
    event_id = evaluate(client, rule["id"], BASE + timedelta(hours=1), start_from=BASE)["opened_event_ids"][0]

    detail = client.get(f"{PREFIX}/anomaly-rules/{rule['id']}").json()
    assert detail["state"]["rule_version"] == 1
    assert detail["state"]["watermark"] == "2026-01-01T11:00:00"
    assert detail["state"]["active_event_id"] == event_id
    assert len(detail["versions"]) == 1
    assert detail["recent_evaluations"][0]["sample_ids"] == sorted([first.id, second.id])

    event = get_event(client, event_id)
    assert event["event"]["trigger_sample_ids"] == sorted([first.id, second.id])
    assert event["event"]["trigger_failure_rate"] == 1.0
    assert event["trigger_windows"][0]["sample_ids"] == sorted([first.id, second.id])

    open_events = client.get(f"{PREFIX}/anomaly-events", params={"status": "open"}).json()
    assert [e["id"] for e in open_events] == [event_id]
    assert client.get(f"{PREFIX}/anomaly-events", params={"status": "resolved"}).json() == []


def test_rule_validation_and_disabled_rule(client, session, resources):
    # 恢复阈值必须小于触发阈值
    resp = client.post(
        f"{PREFIX}/anomaly-rules",
        json={
            "name": "非法规则",
            "robot_model_id": resources.robot_model_id,
            "scene_id": resources.scene_id,
            "skill_id": resources.skill_id,
            "window_size_minutes": 60,
            "min_samples": 2,
            "trigger_threshold": 0.2,
            "recovery_threshold": 0.5,
            "operator": "ops-bot",
        },
    )
    assert resp.status_code == 400

    rule = create_rule(client, resources)
    # 同一组合不允许重复启用规则
    resp = client.post(
        f"{PREFIX}/anomaly-rules",
        json={
            "name": "重复规则",
            "robot_model_id": resources.robot_model_id,
            "scene_id": resources.scene_id,
            "skill_id": resources.skill_id,
            "window_size_minutes": 30,
            "min_samples": 1,
            "trigger_threshold": 0.5,
            "recovery_threshold": 0.2,
            "operator": "ops-bot",
        },
    )
    assert resp.status_code == 400

    # 停用后拒绝评估
    resp = client.put(
        f"{PREFIX}/anomaly-rules/{rule['id']}",
        json={"enabled": False, "changed_by": "王五", "change_reason": "产线停线"},
    )
    assert resp.status_code == 200
    resp = client.post(f"{PREFIX}/anomaly-rules/{rule['id']}/evaluate", json={})
    assert resp.status_code == 400
