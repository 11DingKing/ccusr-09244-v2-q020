from pydantic import BaseModel, Field
from typing import Optional, Any, Dict, List, Literal
from datetime import datetime


class AnomalyRuleCreate(BaseModel):
    name: str = Field(..., max_length=100, description="规则名称")
    robot_model_id: int = Field(..., description="机型ID")
    scene_id: int = Field(..., description="场景ID")
    skill_id: int = Field(..., description="技能ID")
    window_size_minutes: int = Field(..., gt=0, description="样本窗口宽度（分钟）")
    min_samples: int = Field(..., ge=1, description="窗口可评估的最低样本量")
    trigger_threshold: float = Field(..., gt=0, le=1, description="失败率触发阈值")
    recovery_threshold: float = Field(..., ge=0, le=1, description="失败率恢复阈值，须小于触发阈值")
    consecutive_windows: int = Field(1, ge=1, description="连续超阈值多少个窗口才触发")
    enabled: bool = Field(True, description="是否启用")
    operator: str = Field(..., min_length=1, max_length=100, description="创建人")


class AnomalyRuleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    window_size_minutes: Optional[int] = Field(None, gt=0)
    min_samples: Optional[int] = Field(None, ge=1)
    trigger_threshold: Optional[float] = Field(None, gt=0, le=1)
    recovery_threshold: Optional[float] = Field(None, ge=0, le=1)
    consecutive_windows: Optional[int] = Field(None, ge=1)
    enabled: Optional[bool] = None
    changed_by: str = Field(..., min_length=1, max_length=100, description="变更操作者")
    change_reason: str = Field(..., min_length=1, description="变更依据")


class AnomalyRuleResponse(BaseModel):
    id: int
    name: str
    robot_model_id: int
    scene_id: int
    skill_id: int
    window_size_minutes: int
    min_samples: int
    trigger_threshold: float
    recovery_threshold: float
    consecutive_windows: int
    enabled: bool
    version: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AnomalyRuleVersionResponse(BaseModel):
    version: int
    name: str
    robot_model_id: int
    scene_id: int
    skill_id: int
    window_size_minutes: int
    min_samples: int
    trigger_threshold: float
    recovery_threshold: float
    consecutive_windows: int
    enabled: bool
    changed_by: str
    change_reason: str
    effective_at: datetime

    class Config:
        from_attributes = True


class AnomalyRuleStateResponse(BaseModel):
    rule_version: int
    watermark: datetime
    last_run_at: datetime
    active_event_id: Optional[int] = None


class AnomalyWindowEvaluationResponse(BaseModel):
    id: int
    rule_id: int
    rule_version: int
    window_start: datetime
    window_end: datetime
    sample_count: int
    failure_count: int
    failure_rate: Optional[float] = None
    evaluable: bool
    sample_ids: List[int]
    locked: bool = False
    recompute_count: int
    first_computed_at: datetime
    computed_at: datetime

    class Config:
        from_attributes = True


class AnomalyRuleDetailResponse(BaseModel):
    rule: AnomalyRuleResponse
    state: Optional[AnomalyRuleStateResponse] = None
    versions: List[AnomalyRuleVersionResponse]
    recent_evaluations: List[AnomalyWindowEvaluationResponse]


class AnomalyEvaluateRequest(BaseModel):
    until: Optional[datetime] = Field(None, description="评估截止的业务时间，缺省为当前时间")
    start_from: Optional[datetime] = Field(None, description="首次评估的水位线起点，缺省为当前时间")


class AnomalyEvaluateResponse(BaseModel):
    rule_id: int
    rule_version: int
    watermark: datetime
    evaluated_windows: int
    recomputed_windows: int
    locked_windows_skipped: int
    opened_event_ids: List[int]
    resolved_event_ids: List[int]
    active_event_id: Optional[int] = None


class AnomalyEventResponse(BaseModel):
    id: int
    rule_id: int
    rule_version: int
    status: str
    trigger_window_start: datetime
    trigger_window_end: datetime
    trigger_failure_rate: float
    trigger_sample_count: int
    trigger_failure_count: int
    trigger_sample_ids: List[int]
    threshold_snapshot: Dict[str, Any]
    opened_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolve_reason: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AnomalyEventActionCreate(BaseModel):
    action: Literal["confirm", "ignore", "resolve"] = Field(..., description="确认/忽略/恢复")
    operator: str = Field(..., min_length=1, max_length=100, description="操作者")
    reason: str = Field(..., min_length=1, description="操作依据")


class AnomalyEventActionResponse(BaseModel):
    id: int
    event_id: int
    action: str
    operator: str
    reason: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AnomalyEventDetailResponse(BaseModel):
    event: AnomalyEventResponse
    trigger_windows: List[AnomalyWindowEvaluationResponse]
    actions: List[AnomalyEventActionResponse]
