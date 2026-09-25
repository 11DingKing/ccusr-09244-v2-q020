from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    AnomalyEvent,
    AnomalyEventAction,
    AnomalyRule,
    AnomalyRuleState,
    AnomalyRuleVersion,
    AnomalyWindowEvaluation,
    RobotModel,
    Scene,
    Skill,
)
from app.schemas.anomaly import (
    AnomalyEvaluateRequest,
    AnomalyEvaluateResponse,
    AnomalyEventActionCreate,
    AnomalyEventActionResponse,
    AnomalyEventDetailResponse,
    AnomalyEventResponse,
    AnomalyRuleCreate,
    AnomalyRuleDetailResponse,
    AnomalyRuleResponse,
    AnomalyRuleStateResponse,
    AnomalyRuleUpdate,
    AnomalyRuleVersionResponse,
    AnomalyWindowEvaluationResponse,
)
from app.services import anomaly_engine
from app.services.anomaly import AnomalyError, EventAction, window_locked

router = APIRouter()


def get_rule_or_404(rule_id: int, db: Session) -> AnomalyRule:
    rule = db.query(AnomalyRule).filter(AnomalyRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="异常监测规则不存在")
    return rule


def evaluation_response(row: AnomalyWindowEvaluation, locked) -> AnomalyWindowEvaluationResponse:
    return AnomalyWindowEvaluationResponse(
        id=row.id,
        rule_id=row.rule_id,
        rule_version=row.rule_version,
        window_start=row.window_start,
        window_end=row.window_end,
        sample_count=row.sample_count,
        failure_count=row.failure_count,
        failure_rate=row.failure_rate,
        evaluable=row.evaluable,
        sample_ids=row.sample_ids,
        locked=window_locked(
            anomaly_engine.as_utc(row.window_start),
            anomaly_engine.as_utc(row.window_end),
            locked,
        ),
        recompute_count=row.recompute_count,
        first_computed_at=row.first_computed_at,
        computed_at=row.computed_at,
    )


@router.post("/anomaly-rules", response_model=AnomalyRuleResponse, tags=["异常监测"])
def create_anomaly_rule(data: AnomalyRuleCreate, db: Session = Depends(get_db)):
    if not db.query(RobotModel).filter(RobotModel.id == data.robot_model_id).first():
        raise HTTPException(status_code=400, detail="机型不存在")
    if not db.query(Scene).filter(Scene.id == data.scene_id).first():
        raise HTTPException(status_code=400, detail="场景不存在")
    if not db.query(Skill).filter(Skill.id == data.skill_id).first():
        raise HTTPException(status_code=400, detail="技能不存在")
    if db.query(AnomalyRule).filter(AnomalyRule.name == data.name).first():
        raise HTTPException(status_code=400, detail="规则名称已存在")
    if data.enabled:
        duplicate = (
            db.query(AnomalyRule)
            .filter(
                AnomalyRule.robot_model_id == data.robot_model_id,
                AnomalyRule.scene_id == data.scene_id,
                AnomalyRule.skill_id == data.skill_id,
                AnomalyRule.enabled == True,
            )
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail=f"同一机型、场景、技能组合已存在启用规则：{duplicate.name}")

    rule = AnomalyRule(
        name=data.name,
        robot_model_id=data.robot_model_id,
        scene_id=data.scene_id,
        skill_id=data.skill_id,
        window_size_minutes=data.window_size_minutes,
        min_samples=data.min_samples,
        trigger_threshold=data.trigger_threshold,
        recovery_threshold=data.recovery_threshold,
        consecutive_windows=data.consecutive_windows,
        enabled=data.enabled,
        version=1,
    )
    try:
        anomaly_engine.rule_spec(rule)
    except AnomalyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.add(rule)
    db.flush()
    anomaly_engine.snapshot_version(db, rule, data.operator, "创建规则")
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/anomaly-rules", response_model=List[AnomalyRuleResponse], tags=["异常监测"])
def list_anomaly_rules(
    enabled: Optional[bool] = Query(None, description="按启用状态过滤"),
    db: Session = Depends(get_db),
):
    query = db.query(AnomalyRule)
    if enabled is not None:
        query = query.filter(AnomalyRule.enabled == enabled)
    return query.order_by(AnomalyRule.id).all()


@router.get("/anomaly-rules/{rule_id}", response_model=AnomalyRuleDetailResponse, tags=["异常监测"])
def get_anomaly_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = get_rule_or_404(rule_id, db)
    versions = (
        db.query(AnomalyRuleVersion)
        .filter(AnomalyRuleVersion.rule_id == rule.id)
        .order_by(AnomalyRuleVersion.version)
        .all()
    )
    state = (
        db.query(AnomalyRuleState)
        .filter(AnomalyRuleState.rule_id == rule.id, AnomalyRuleState.rule_version == rule.version)
        .first()
    )
    active = anomaly_engine.active_event(db, rule)
    evaluations = (
        db.query(AnomalyWindowEvaluation)
        .filter(
            AnomalyWindowEvaluation.rule_id == rule.id,
            AnomalyWindowEvaluation.rule_version == rule.version,
        )
        .order_by(AnomalyWindowEvaluation.window_start.desc())
        .limit(20)
        .all()
    )
    evaluations.reverse()
    locked = anomaly_engine.locked_intervals(db, rule.id)
    return AnomalyRuleDetailResponse(
        rule=rule,
        state=AnomalyRuleStateResponse(
            rule_version=state.rule_version,
            watermark=state.watermark,
            last_run_at=state.last_run_at,
            active_event_id=active.id if active else None,
        )
        if state
        else None,
        versions=versions,
        recent_evaluations=[evaluation_response(row, locked) for row in evaluations],
    )


@router.put("/anomaly-rules/{rule_id}", response_model=AnomalyRuleResponse, tags=["异常监测"])
def update_anomaly_rule(rule_id: int, data: AnomalyRuleUpdate, db: Session = Depends(get_db)):
    rule = get_rule_or_404(rule_id, db)
    changes = data.model_dump(exclude_unset=True, exclude={"changed_by", "change_reason"})
    if not changes:
        raise HTTPException(status_code=400, detail="没有需要变更的字段")
    if "name" in changes and changes["name"] != rule.name:
        existing = (
            db.query(AnomalyRule)
            .filter(AnomalyRule.name == changes["name"], AnomalyRule.id != rule.id)
            .first()
        )
        if existing:
            raise HTTPException(status_code=400, detail="规则名称已存在")
    try:
        anomaly_engine.apply_rule_change(db, rule, changes, data.changed_by, data.change_reason)
    except AnomalyError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return rule


@router.get("/anomaly-rules/{rule_id}/versions", response_model=List[AnomalyRuleVersionResponse], tags=["异常监测"])
def list_anomaly_rule_versions(rule_id: int, db: Session = Depends(get_db)):
    get_rule_or_404(rule_id, db)
    return (
        db.query(AnomalyRuleVersion)
        .filter(AnomalyRuleVersion.rule_id == rule_id)
        .order_by(AnomalyRuleVersion.version)
        .all()
    )


@router.get(
    "/anomaly-rules/{rule_id}/evaluations",
    response_model=List[AnomalyWindowEvaluationResponse],
    tags=["异常监测"],
)
def list_anomaly_rule_evaluations(
    rule_id: int,
    version: Optional[int] = Query(None, description="规则版本，缺省为当前版本"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    rule = get_rule_or_404(rule_id, db)
    rows = (
        db.query(AnomalyWindowEvaluation)
        .filter(
            AnomalyWindowEvaluation.rule_id == rule.id,
            AnomalyWindowEvaluation.rule_version == (version or rule.version),
        )
        .order_by(AnomalyWindowEvaluation.window_start)
        .limit(limit)
        .all()
    )
    locked = anomaly_engine.locked_intervals(db, rule.id)
    return [evaluation_response(row, locked) for row in rows]


@router.post("/anomaly-rules/{rule_id}/evaluate", response_model=AnomalyEvaluateResponse, tags=["异常监测"])
def evaluate_anomaly_rule(rule_id: int, data: AnomalyEvaluateRequest, db: Session = Depends(get_db)):
    rule = get_rule_or_404(rule_id, db)
    if not rule.enabled:
        raise HTTPException(status_code=400, detail="规则已停用，无法评估")
    try:
        report = anomaly_engine.evaluate_rule(db, rule, until=data.until, start_from=data.start_from)
    except AnomalyError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    active = anomaly_engine.active_event(db, rule)
    return AnomalyEvaluateResponse(
        rule_id=rule.id,
        rule_version=report.rule_version,
        watermark=report.watermark,
        evaluated_windows=len(report.evaluated_windows),
        recomputed_windows=len(report.recomputed_windows),
        locked_windows_skipped=report.locked_windows_skipped,
        opened_event_ids=report.opened_event_ids,
        resolved_event_ids=report.resolved_event_ids,
        active_event_id=active.id if active else None,
    )


@router.get("/anomaly-events", response_model=List[AnomalyEventResponse], tags=["异常监测"])
def list_anomaly_events(
    status: Optional[str] = Query(None, description="按状态过滤：open/acknowledged/suppressed/resolved"),
    rule_id: Optional[int] = Query(None, description="按规则过滤"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(AnomalyEvent)
    if status:
        query = query.filter(AnomalyEvent.status == status)
    if rule_id:
        query = query.filter(AnomalyEvent.rule_id == rule_id)
    return query.order_by(AnomalyEvent.id.desc()).limit(limit).all()


@router.get("/anomaly-events/{event_id}", response_model=AnomalyEventDetailResponse, tags=["异常监测"])
def get_anomaly_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(AnomalyEvent).filter(AnomalyEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="异常事件不存在")
    windows = (
        db.query(AnomalyWindowEvaluation)
        .filter(
            AnomalyWindowEvaluation.rule_id == event.rule_id,
            AnomalyWindowEvaluation.rule_version == event.rule_version,
            AnomalyWindowEvaluation.window_start >= event.trigger_window_start,
            AnomalyWindowEvaluation.window_start < event.trigger_window_end,
        )
        .order_by(AnomalyWindowEvaluation.window_start)
        .all()
    )
    actions = (
        db.query(AnomalyEventAction)
        .filter(AnomalyEventAction.event_id == event.id)
        .order_by(AnomalyEventAction.id)
        .all()
    )
    locked = anomaly_engine.locked_intervals(db, event.rule_id)
    return AnomalyEventDetailResponse(
        event=event,
        trigger_windows=[evaluation_response(row, locked) for row in windows],
        actions=actions,
    )


@router.post(
    "/anomaly-events/{event_id}/actions",
    response_model=AnomalyEventActionResponse,
    tags=["异常监测"],
)
def create_anomaly_event_action(event_id: int, data: AnomalyEventActionCreate, db: Session = Depends(get_db)):
    event = db.query(AnomalyEvent).filter(AnomalyEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="异常事件不存在")
    try:
        return anomaly_engine.record_event_action(db, event, EventAction(data.action), data.operator, data.reason)
    except AnomalyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
