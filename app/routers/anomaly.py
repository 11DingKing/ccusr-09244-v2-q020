"""可解释异常监测接口：规则管理、评估触发、事件查询与人工处置。"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnomalyEvent, AnomalyEventAction, AnomalyRule, AnomalyWindowResult
from app.schemas.anomaly import (
    AnomalyActionRequest,
    AnomalyEvaluateRequest,
    AnomalyEvaluateResponse,
    AnomalyEventResponse,
    AnomalyRuleCreate,
    AnomalyRuleResponse,
    AnomalyRuleUpdate,
    AnomalyWindowResponse,
)
from app.services.anomaly import AnomalyConfigError
from app.services import anomaly_monitor as monitor

router = APIRouter()


def _get_rule_or_404(db: Session, rule_id: int) -> AnomalyRule:
    rule = db.query(AnomalyRule).filter(AnomalyRule.id == rule_id).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="监测规则不存在")
    return rule


def _get_event_or_404(db: Session, event_id: int) -> AnomalyEvent:
    event = db.query(AnomalyEvent).filter(AnomalyEvent.id == event_id).first()
    if event is None:
        raise HTTPException(status_code=404, detail="异常事件不存在")
    return event


def _serialize_event(event: AnomalyEvent) -> AnomalyEventResponse:
    return AnomalyEventResponse(
        id=event.id,
        rule_id=event.rule_id,
        rule_revision=event.rule_revision,
        rule_snapshot=event.rule_snapshot,
        status=event.status,
        first_trigger_window_start=event.first_trigger_window_start,
        last_window_start=event.last_window_start,
        first_triggered_at=event.first_triggered_at,
        last_change_at=event.last_change_at,
        last_evaluated_at=event.last_evaluated_at,
        confirmed_through=event.confirmed_through,
        trigger_sample_from=event.trigger_sample_from,
        trigger_sample_to=event.trigger_sample_to,
        trigger_total=event.trigger_total,
        trigger_failures=event.trigger_failures,
        trigger_failure_rate=event.trigger_failure_rate,
        trigger_threshold=event.trigger_threshold,
        latest_sample_from=event.latest_sample_from,
        latest_sample_to=event.latest_sample_to,
        latest_total=event.latest_total,
        latest_failures=event.latest_failures,
        latest_failure_rate=event.latest_failure_rate,
        rule_name=event.rule.name if event.rule else None,
        robot_model_id=event.rule.robot_model_id if event.rule else None,
        scene_id=event.rule.scene_id if event.rule else None,
        skill_id=event.rule.skill_id if event.rule else None,
        actions=list(event.actions),
    )


@router.post("/anomaly/rules", response_model=AnomalyRuleResponse, tags=["异常监测-规则"])
def create_anomaly_rule(
    payload: AnomalyRuleCreate,
    db: Session = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    fields = payload.model_dump(exclude={"change_reason", "effective_start_at"})
    try:
        rule = monitor.create_rule(
            db,
            operator=x_operator,
            start_at=payload.effective_start_at,
            change_reason=payload.change_reason,
            **fields,
        )
    except AnomalyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rule


@router.get("/anomaly/rules", response_model=List[AnomalyRuleResponse], tags=["异常监测-规则"])
def list_anomaly_rules(
    is_active: Optional[bool] = Query(None, description="按启用状态过滤"),
    robot_model_id: Optional[int] = Query(None),
    scene_id: Optional[int] = Query(None),
    skill_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(AnomalyRule)
    if is_active is not None:
        query = query.filter(AnomalyRule.is_active == is_active)
    if robot_model_id is not None:
        query = query.filter(AnomalyRule.robot_model_id == robot_model_id)
    if scene_id is not None:
        query = query.filter(AnomalyRule.scene_id == scene_id)
    if skill_id is not None:
        query = query.filter(AnomalyRule.skill_id == skill_id)
    return query.order_by(AnomalyRule.id.asc()).all()


@router.get("/anomaly/rules/{rule_id}", response_model=AnomalyRuleResponse, tags=["异常监测-规则"])
def get_anomaly_rule(rule_id: int, db: Session = Depends(get_db)):
    return _get_rule_or_404(db, rule_id)


@router.put("/anomaly/rules/{rule_id}", response_model=AnomalyRuleResponse, tags=["异常监测-规则"])
def update_anomaly_rule(
    rule_id: int,
    payload: AnomalyRuleUpdate,
    db: Session = Depends(get_db),
    x_operator: Optional[str] = Header(None, alias="X-Operator"),
):
    rule = _get_rule_or_404(db, rule_id)
    changes = payload.model_dump(exclude_unset=True, exclude={"effective_start_at"})
    try:
        return monitor.update_rule(
            db, rule, changes,
            operator=x_operator,
            start_at=payload.effective_start_at,
        )
    except AnomalyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/anomaly/rules/{rule_id}/evaluate", response_model=AnomalyEvaluateResponse, tags=["异常监测-评估"])
def evaluate_anomaly_rule(
    rule_id: int,
    payload: Optional[AnomalyEvaluateRequest] = None,
    db: Session = Depends(get_db),
):
    rule = _get_rule_or_404(db, rule_id)
    kwargs = payload.model_dump() if payload else {}
    try:
        report = monitor.evaluate_rule(db, rule, **kwargs)
    except AnomalyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AnomalyEvaluateResponse(**report.as_dict())


@router.post("/anomaly/evaluate-all", response_model=List[AnomalyEvaluateResponse], tags=["异常监测-评估"])
def evaluate_all_anomaly_rules(db: Session = Depends(get_db)):
    reports = []
    for rule in db.query(AnomalyRule).filter(AnomalyRule.is_active.is_(True)).order_by(AnomalyRule.id.asc()).all():
        reports.append(AnomalyEvaluateResponse(**monitor.evaluate_rule(db, rule).as_dict()))
    return reports


@router.get("/anomaly/rules/{rule_id}/windows", response_model=List[AnomalyWindowResponse], tags=["异常监测-评估"])
def list_rule_windows(
    rule_id: int,
    confirmed: Optional[bool] = Query(None, description="按确认状态过滤"),
    db: Session = Depends(get_db),
):
    _get_rule_or_404(db, rule_id)
    query = db.query(AnomalyWindowResult).filter(AnomalyWindowResult.rule_id == rule_id)
    if confirmed is not None:
        query = query.filter(AnomalyWindowResult.confirmed == confirmed)
    return query.order_by(AnomalyWindowResult.window_start.asc()).all()


@router.get("/anomaly/events", response_model=List[AnomalyEventResponse], tags=["异常监测-事件"])
def list_anomaly_events(
    status: Optional[str] = Query(None, description="open / ignored / recovered / retracted"),
    rule_id: Optional[int] = Query(None),
    robot_model_id: Optional[int] = Query(None),
    scene_id: Optional[int] = Query(None),
    skill_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(AnomalyEvent).join(AnomalyRule, AnomalyEvent.rule_id == AnomalyRule.id)
    if status:
        query = query.filter(AnomalyEvent.status == status)
    if rule_id is not None:
        query = query.filter(AnomalyEvent.rule_id == rule_id)
    if robot_model_id is not None:
        query = query.filter(AnomalyRule.robot_model_id == robot_model_id)
    if scene_id is not None:
        query = query.filter(AnomalyRule.scene_id == scene_id)
    if skill_id is not None:
        query = query.filter(AnomalyRule.skill_id == skill_id)
    events = query.order_by(AnomalyEvent.first_triggered_at.desc(), AnomalyEvent.id.desc()).all()
    return [_serialize_event(event) for event in events]


@router.get("/anomaly/events/{event_id}", response_model=AnomalyEventResponse, tags=["异常监测-事件"])
def get_anomaly_event(event_id: int, db: Session = Depends(get_db)):
    return _serialize_event(_get_event_or_404(db, event_id))


@router.post("/anomaly/events/{event_id}/confirm", response_model=AnomalyEventResponse, tags=["异常监测-事件"])
def confirm_anomaly_event(
    event_id: int,
    payload: AnomalyActionRequest,
    db: Session = Depends(get_db),
):
    event = _get_event_or_404(db, event_id)
    try:
        event = monitor.confirm_event(
            db,
            event,
            operator=payload.operator,
            reason=payload.reason,
            through_window_start=payload.through_window_start,
        )
    except AnomalyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_event(event)


@router.post("/anomaly/events/{event_id}/ignore", response_model=AnomalyEventResponse, tags=["异常监测-事件"])
def ignore_anomaly_event(event_id: int, payload: AnomalyActionRequest, db: Session = Depends(get_db)):
    event = _get_event_or_404(db, event_id)
    try:
        event = monitor.ignore_event(db, event, operator=payload.operator, reason=payload.reason)
    except AnomalyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_event(event)


@router.post("/anomaly/events/{event_id}/recover", response_model=AnomalyEventResponse, tags=["异常监测-事件"])
def recover_anomaly_event(event_id: int, payload: AnomalyActionRequest, db: Session = Depends(get_db)):
    event = _get_event_or_404(db, event_id)
    try:
        event = monitor.recover_event(db, event, operator=payload.operator, reason=payload.reason)
    except AnomalyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_event(event)


@router.get("/anomaly/events/{event_id}/actions", tags=["异常监测-事件"])
def list_anomaly_event_actions(event_id: int, db: Session = Depends(get_db)):
    _get_event_or_404(db, event_id)
    rows = (
        db.query(AnomalyEventAction)
        .filter(AnomalyEventAction.event_id == event_id)
        .order_by(AnomalyEventAction.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "action": row.action,
            "operator": row.operator,
            "reason": row.reason,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in rows
    ]
