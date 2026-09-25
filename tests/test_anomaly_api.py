"""异常监测 HTTP 接口测试：规则校验、评估、事件样本范围展示与人工处置。"""

from datetime import datetime, timedelta, timezone

from app.models import Annotation, OperationData, RobotModel, Scene, Skill
from app.services import anomaly_monitor as monitor

UTC = timezone.utc
API = "/api/v1"
T0 = datetime(2025, 6, 1, tzinfo=UTC)


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def seed(db, failures=4, successes=1):
    model = RobotModel(name="RM-API", manufacturer="M")
    scene = Scene(name="场景API", category="测试")
    skill = Skill(name="技能API", category="测试")
    db.add_all([model, scene, skill])
    db.flush()
    for i in range(failures):
        op = OperationData(
            robot_model_id=model.id, scene_id=scene.id, skill_id=skill.id,
            motion_trajectory={"waypoints": [1]}, perception_records={"c": 1},
            timestamp_start=_naive(T0 + timedelta(minutes=2 + i)),
            timestamp_end=_naive(T0 + timedelta(minutes=3 + i)),
        )
        db.add(op)
        db.flush()
        db.add(Annotation(operation_data_id=op.id, is_success=False, failure_category="其他"))
    for i in range(successes):
        op = OperationData(
            robot_model_id=model.id, scene_id=scene.id, skill_id=skill.id,
            motion_trajectory={"waypoints": [1]}, perception_records={"c": 1},
            timestamp_start=_naive(T0 + timedelta(minutes=20 + i)),
            timestamp_end=_naive(T0 + timedelta(minutes=21 + i)),
        )
        db.add(op)
        db.flush()
        db.add(Annotation(operation_data_id=op.id, is_success=True))
    db.commit()
    return model.id, scene.id, skill.id


def rule_payload(ids, **overrides):
    payload = {
        "name": "接口监测规则",
        "robot_model_id": ids[0], "scene_id": ids[1], "skill_id": ids[2],
        "window_minutes": 60, "step_minutes": 60, "minimum_sample_count": 5,
        "trigger_threshold": 0.5, "recovery_threshold": 0.2,
        "effective_start_at": T0.isoformat(),
    }
    payload.update(overrides)
    return payload


def test_rule_lifecycle_and_event_query_explains_samples(client, db):
    ids = seed(db)

    # 参数校验：恢复阈值必须低于触发阈值
    bad = client.post(f"{API}/anomaly/rules", json=rule_payload(ids, recovery_threshold=0.6))
    assert bad.status_code == 400

    created = client.post(
        f"{API}/anomaly/rules",
        json=rule_payload(ids),
        headers={"X-Operator": "ops-jia"},
    )
    assert created.status_code == 200
    rule_id = created.json()["id"]
    assert created.json()["revision"] == 1

    as_of = (T0 + timedelta(hours=1, minutes=1)).isoformat()
    report = client.post(f"{API}/anomaly/rules/{rule_id}/evaluate", json={"as_of": as_of})
    assert report.status_code == 200
    body = report.json()
    assert body["windows_total"] == 1
    assert len(body["triggered_event_ids"]) == 1
    event_id = body["triggered_event_ids"][0]

    # 查询接口展示触发计算的样本范围与当前状态
    event = client.get(f"{API}/anomaly/events/{event_id}").json()
    assert event["status"] == "open"
    assert event["trigger_total"] == 5
    assert event["trigger_failures"] == 4
    assert event["trigger_failure_rate"] == 0.8
    assert event["trigger_sample_from"].startswith("2025-06-01T00:00:00")
    assert event["trigger_sample_to"].startswith("2025-06-01T01:00:00")
    assert event["rule_snapshot"]["window_minutes"] == 60
    assert event["rule_name"] == "接口监测规则"
    assert event["actions"][0]["action"] == "triggered"

    listed = client.get(f"{API}/anomaly/events", params={"status": "open"}).json()
    assert len(listed) == 1 and listed[0]["id"] == event_id

    # 窗口明细接口
    windows = client.get(f"{API}/anomaly/rules/{rule_id}/windows").json()
    assert windows[0]["total"] == 5 and windows[0]["signal"] == "breach"


def test_manual_actions_record_operator_and_reason(client, db):
    ids = seed(db)
    rule_id = client.post(f"{API}/anomaly/rules", json=rule_payload(ids)).json()["id"]
    as_of = (T0 + timedelta(hours=1, minutes=1)).isoformat()
    event_id = client.post(
        f"{API}/anomaly/rules/{rule_id}/evaluate", json={"as_of": as_of}
    ).json()["triggered_event_ids"][0]

    # 确认：锁定窗口并记录操作者与依据
    confirmed = client.post(
        f"{API}/anomaly/events/{event_id}/confirm",
        json={"operator": "ops-yi", "reason": "ticket-42 confirmed", "through_window_start": T0.isoformat()},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_through"].startswith("2025-06-01T01:00:00")

    windows = client.get(f"{API}/anomaly/rules/{rule_id}/windows", params={"confirmed": True}).json()
    assert len(windows) == 1 and windows[0]["confirmed_by"] == "ops-yi"

    actions = client.get(f"{API}/anomaly/events/{event_id}/actions").json()
    kinds = {(a["action"], a["operator"], a["reason"]) for a in actions}
    assert ("confirm", "ops-yi", "ticket-42 confirmed") in kinds

    # 缺少操作者的请求被拒绝
    missing = client.post(
        f"{API}/anomaly/events/{event_id}/ignore", json={"reason": "x"},
    )
    assert missing.status_code == 422

    ignored = client.post(
        f"{API}/anomaly/events/{event_id}/ignore",
        json={"operator": "ops-bing", "reason": "planned fluctuation after review"},
    )
    # 已确认（open）事件允许继续忽略
    assert ignored.status_code == 200
    assert ignored.json()["status"] == "ignored"


def test_rule_change_creates_revision_and_keeps_old_event(client, db):
    ids = seed(db)
    rule_id = client.post(f"{API}/anomaly/rules", json=rule_payload(ids)).json()["id"]
    as_of = (T0 + timedelta(hours=1, minutes=1)).isoformat()
    event_id = client.post(
        f"{API}/anomaly/rules/{rule_id}/evaluate", json={"as_of": as_of}
    ).json()["triggered_event_ids"][0]

    updated = client.put(
        f"{API}/anomaly/rules/{rule_id}",
        json={"trigger_threshold": 0.3, "change_reason": "周会调严"},
        headers={"X-Operator": "ops-jia"},
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2

    # 再评估：旧事件保持在版本 1，不被新阈值覆盖
    client.post(f"{API}/anomaly/rules/{rule_id}/evaluate", json={"as_of": as_of})
    event = client.get(f"{API}/anomaly/events/{event_id}").json()
    assert event["rule_revision"] == 1
    assert event["rule_snapshot"]["trigger_threshold"] == 0.5
    assert event["status"] == "open"
