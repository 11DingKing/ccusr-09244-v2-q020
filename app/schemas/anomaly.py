from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class AnomalyRuleBase(BaseModel):
    name: str = Field(..., max_length=200, description="规则名称")
    robot_model_id: Optional[int] = Field(None, description="机型ID，空表示全部机型")
    scene_id: Optional[int] = Field(None, description="场景ID，空表示全部场景")
    skill_id: Optional[int] = Field(None, description="技能ID，空表示全部技能")

    window_minutes: int = Field(..., ge=1, description="样本窗口宽度（分钟）")
    step_minutes: int = Field(..., ge=1, description="窗口步长（分钟），不大于窗口宽度")
    minimum_sample_count: int = Field(..., ge=1, description="窗口最低样本量")

    trigger_threshold: float = Field(..., gt=0.0, le=1.0, description="失败率触发阈值")
    recovery_threshold: float = Field(..., ge=0.0, lt=1.0, description="失败率恢复阈值（须低于触发阈值）")
    trigger_consecutive_windows: int = Field(1, ge=1, description="连续越限窗口数")
    recovery_consecutive_windows: int = Field(1, ge=1, description="连续恢复窗口数")

    effective_start_at: Optional[datetime] = Field(None, description="规则生效（开始监测）的业务时间，默认当前时刻")
    change_reason: Optional[str] = Field(None, description="变更依据/说明")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("规则名称不能为空")
        return value


class AnomalyRuleCreate(AnomalyRuleBase):
    pass


class AnomalyRuleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    robot_model_id: Optional[int] = None
    scene_id: Optional[int] = None
    skill_id: Optional[int] = None
    window_minutes: Optional[int] = Field(None, ge=1)
    step_minutes: Optional[int] = Field(None, ge=1)
    minimum_sample_count: Optional[int] = Field(None, ge=1)
    trigger_threshold: Optional[float] = Field(None, gt=0.0, le=1.0)
    recovery_threshold: Optional[float] = Field(None, ge=0.0, lt=1.0)
    trigger_consecutive_windows: Optional[int] = Field(None, ge=1)
    recovery_consecutive_windows: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None
    change_reason: Optional[str] = None
    effective_start_at: Optional[datetime] = Field(None, description="口径变更后新版本的生效业务时间，默认当前时刻")


class AnomalyRuleResponse(BaseModel):
    id: int
    name: str
    robot_model_id: Optional[int] = None
    scene_id: Optional[int] = None
    skill_id: Optional[int] = None
    window_minutes: int
    step_minutes: int
    minimum_sample_count: int
    trigger_threshold: float
    recovery_threshold: float
    trigger_consecutive_windows: int
    recovery_consecutive_windows: int
    revision: int
    revision_start_at: datetime
    last_evaluated_at: Optional[datetime] = None
    is_active: bool
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    change_reason: Optional[str] = None

    class Config:
        from_attributes = True


class AnomalyActionResponse(BaseModel):
    id: int
    action: str
    operator: str
    reason: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AnomalyEventResponse(BaseModel):
    id: int
    rule_id: int
    rule_revision: int
    rule_snapshot: Dict[str, Any]
    status: str

    first_trigger_window_start: datetime
    last_window_start: datetime
    first_triggered_at: datetime
    last_change_at: datetime
    last_evaluated_at: datetime
    confirmed_through: Optional[datetime] = None

    # 触发本次异常的样本范围与指标
    trigger_sample_from: datetime
    trigger_sample_to: datetime
    trigger_total: int
    trigger_failures: int
    trigger_failure_rate: float
    trigger_threshold: float

    # 最近一次评估的样本范围与指标
    latest_sample_from: datetime
    latest_sample_to: datetime
    latest_total: int
    latest_failures: int
    latest_failure_rate: float

    # 查询时附带的规则可读信息
    rule_name: Optional[str] = None
    robot_model_id: Optional[int] = None
    scene_id: Optional[int] = None
    skill_id: Optional[int] = None

    actions: List[AnomalyActionResponse] = []

    class Config:
        from_attributes = True


class AnomalyWindowResponse(BaseModel):
    window_start: datetime
    window_end: datetime
    total: int
    failures: int
    failure_rate: Optional[float] = None
    signal: Optional[str] = None
    confirmed: bool
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AnomalyActionRequest(BaseModel):
    operator: str = Field(..., min_length=1, max_length=100, description="操作者")
    reason: Optional[str] = Field(None, description="操作依据/说明")
    through_window_start: Optional[datetime] = Field(
        None, description="确认时锁定到该窗口起点（含），为空则锁定当前全部已计算窗口"
    )


class AnomalyEvaluateRequest(BaseModel):
    as_of: Optional[datetime] = Field(None, description="评估时点（业务时间），默认当前时刻")
    from_time: Optional[datetime] = Field(None, description="评估起点，默认规则当前版本生效时刻")


class AnomalyEvaluateResponse(BaseModel):
    rule_id: int
    revision: int
    as_of: datetime
    range_start: datetime
    windows_total: int
    windows_recomputed: int
    triggered_event_ids: List[int]
    recovered_event_ids: List[int]
    retracted_event_ids: List[int]
    superseded_event_ids: List[int] = []
