from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Boolean, JSON, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class RobotModel(Base):
    __tablename__ = "robot_models"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    manufacturer = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    capabilities = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    operations = relationship("OperationData", back_populates="robot_model")
    datasets = relationship("Dataset", back_populates="robot_model")


class Scene(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    environment_tags = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operations = relationship("OperationData", back_populates="scene")
    datasets = relationship("Dataset", back_populates="scene")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operations = relationship("OperationData", back_populates="skill")


class OperationData(Base):
    __tablename__ = "operation_data"

    id = Column(Integer, primary_key=True, index=True)
    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=False, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=False, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False, index=True)
    robot_serial = Column(String(100), nullable=True, index=True)

    motion_trajectory = Column(JSON, nullable=False)
    perception_records = Column(JSON, nullable=False)
    grasp_result = Column(JSON, nullable=True)

    timestamp_start = Column(DateTime(timezone=True), nullable=False)
    timestamp_end = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=True)

    environment_conditions = Column(JSON, nullable=True)
    hardware_status = Column(JSON, nullable=True)

    quality_score = Column(Float, nullable=True)
    completeness_score = Column(Float, nullable=True)
    data_grade = Column(String(10), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    robot_model = relationship("RobotModel", back_populates="operations")
    scene = relationship("Scene", back_populates="operations")
    skill = relationship("Skill", back_populates="operations")
    annotation = relationship("Annotation", back_populates="operation_data", uselist=False, cascade="all, delete-orphan")
    dataset_items = relationship("DatasetItem", back_populates="operation_data", cascade="all, delete-orphan")


class Annotation(Base):
    __tablename__ = "annotations"

    id = Column(Integer, primary_key=True, index=True)
    operation_data_id = Column(Integer, ForeignKey("operation_data.id"), nullable=False, unique=True, index=True)

    is_success = Column(Boolean, nullable=False, index=True)
    failure_category = Column(String(50), nullable=True, index=True)
    failure_subcategory = Column(String(100), nullable=True)
    failure_description = Column(Text, nullable=True)

    annotator = Column(String(100), nullable=True)
    annotation_time = Column(DateTime(timezone=True), server_default=func.now())
    review_status = Column(String(20), default="pending", index=True)
    reviewer = Column(String(100), nullable=True)
    review_notes = Column(Text, nullable=True)

    annotation_quality_score = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    operation_data = relationship("OperationData", back_populates="annotation")


class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=True)
    version = Column(String(20), default="1.0")

    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=False, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=False, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True, index=True)

    owner_team = Column(String(100), nullable=False)
    contact_person = Column(String(100), nullable=True)
    review_status = Column(String(20), default="draft", index=True)
    is_published = Column(Boolean, default=False, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)

    current_version = Column(Integer, default=1)

    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    annotation_complete_rate = Column(Float, default=0.0)
    average_quality_score = Column(Float, nullable=True)
    reuse_count = Column(Integer, default=0, index=True)

    data_grade = Column(String(10), nullable=True, index=True)
    tags = Column(JSON, nullable=True)
    license_info = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    robot_model = relationship("RobotModel", back_populates="datasets")
    scene = relationship("Scene", back_populates="datasets")
    items = relationship("DatasetItem", back_populates="dataset", cascade="all, delete-orphan")
    reuse_records = relationship("DatasetReuse", back_populates="dataset", cascade="all, delete-orphan")
    versions = relationship("DatasetVersion", back_populates="dataset", cascade="all, delete-orphan")
    reviews = relationship("DatasetReview", back_populates="dataset", cascade="all, delete-orphan")
    subscriptions = relationship("DatasetSubscription", back_populates="dataset", cascade="all, delete-orphan")


class DatasetItem(Base):
    __tablename__ = "dataset_items"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    operation_data_id = Column(Integer, ForeignKey("operation_data.id"), nullable=False, index=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="items")
    operation_data = relationship("OperationData", back_populates="dataset_items")


class DatasetReuse(Base):
    __tablename__ = "dataset_reuses"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    reusing_team = Column(String(100), nullable=False)
    purpose = Column(String(200), nullable=True)
    project_name = Column(String(200), nullable=True)
    reuse_date = Column(DateTime(timezone=True), server_default=func.now())
    notes = Column(Text, nullable=True)

    dataset = relationship("Dataset", back_populates="reuse_records")
    version = relationship("DatasetVersion", back_populates="reuse_records")


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    version_label = Column(String(20), nullable=False)
    change_description = Column(Text, nullable=True)

    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    annotation_complete_rate = Column(Float, default=0.0)
    average_quality_score = Column(Float, nullable=True)
    data_grade = Column(String(10), nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="versions")
    reuse_records = relationship("DatasetReuse", back_populates="version")


class DatasetReview(Base):
    __tablename__ = "dataset_reviews"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    action = Column(String(20), nullable=False, index=True)
    reviewer = Column(String(100), nullable=True)
    review_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="reviews")
    version = relationship("DatasetVersion")


class DatasetSubscription(Base):
    __tablename__ = "dataset_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    subscriber_team = Column(String(100), nullable=False)
    contact_person = Column(String(100), nullable=True)
    notify_on_new_version = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="subscriptions")


class AnomalyRule(Base):
    """异常监测规则：按机型/场景/技能组合定义失败率窗口与抑制参数。"""
    __tablename__ = "anomaly_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)

    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=True, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=True, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True, index=True)

    # 窗口宽度/步长以分钟为单位；窗口按作业业务时间（timestamp_start）对齐。
    window_minutes = Column(Integer, nullable=False)
    step_minutes = Column(Integer, nullable=False)
    minimum_sample_count = Column(Integer, nullable=False)

    # 失败率触发阈值与恢复阈值（恢复阈值形成迟滞带，避免在阈值附近反复开关）。
    trigger_threshold = Column(Float, nullable=False)
    recovery_threshold = Column(Float, nullable=False)
    # 连续越限窗口数：触发需要连续 trigger_consecutive_windows 个窗口越限。
    trigger_consecutive_windows = Column(Integer, nullable=False, default=1)
    recovery_consecutive_windows = Column(Integer, nullable=False, default=1)

    # 每次修改规则参数自增；事件保存触发时的版本，规则变更不覆盖旧事件。
    revision = Column(Integer, nullable=False, default=1)
    revision_start_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    retired_at = Column(DateTime(timezone=True), nullable=True)
    change_reason = Column(Text, nullable=True)

    events = relationship("AnomalyEvent", back_populates="rule", cascade="all, delete-orphan")
    revisions = relationship("AnomalyRuleRevision", back_populates="rule", cascade="all, delete-orphan")


class AnomalyRuleRevision(Base):
    """规则参数版本流水：每个版本的生效起点和参数快照。"""
    __tablename__ = "anomaly_rule_revisions"
    __table_args__ = (
        UniqueConstraint("rule_id", "revision", name="uq_anomaly_rule_revision"),
    )

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(Integer, ForeignKey("anomaly_rules.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    snapshot = Column(JSON, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    created_by = Column(String(100), nullable=True)
    change_reason = Column(Text, nullable=True)
    # 已纳入计算的最大数据摄入时间（作业/标注的创建或更新时刻），
    # 用于把迟到数据触发的重算限定在受影响窗口。
    samples_watermark = Column(DateTime(timezone=True), nullable=True)

    rule = relationship("AnomalyRule", back_populates="revisions")


class AnomalyEvent(Base):
    """一次异常的完整生命周期；规则变更不会覆盖或改写旧事件。"""
    __tablename__ = "anomaly_events"
    __table_args__ = (
        UniqueConstraint("rule_id", "rule_revision", "first_trigger_window_start", name="uq_anomaly_event_first_window"),
    )

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(Integer, ForeignKey("anomaly_rules.id"), nullable=False, index=True)
    # 触发时规则参数的快照（规则之后被修改也不影响本事件的解释口径）。
    rule_revision = Column(Integer, nullable=False)
    rule_snapshot = Column(JSON, nullable=False)

    status = Column(String(20), nullable=False, index=True)  # open / suppressed / ignored / recovered
    first_trigger_window_start = Column(DateTime(timezone=True), nullable=False)
    last_window_start = Column(DateTime(timezone=True), nullable=False)
    first_triggered_at = Column(DateTime(timezone=True), nullable=False)
    last_change_at = Column(DateTime(timezone=True), nullable=False)
    last_evaluated_at = Column(DateTime(timezone=True), nullable=False)
    # 已确认（confirmed）的窗口边界；确认边界之前的窗口不再因迟到数据重算。
    confirmed_through = Column(DateTime(timezone=True), nullable=True)
    suppress_until_count = Column(Integer, nullable=True)

    # 触发时与最近一次评估的样本范围与指标（查询接口直接展示）。
    trigger_sample_from = Column(DateTime(timezone=True), nullable=False)
    trigger_sample_to = Column(DateTime(timezone=True), nullable=False)
    trigger_total = Column(Integer, nullable=False)
    trigger_failures = Column(Integer, nullable=False)
    trigger_failure_rate = Column(Float, nullable=False)
    trigger_threshold = Column(Float, nullable=False)
    latest_sample_from = Column(DateTime(timezone=True), nullable=False)
    latest_sample_to = Column(DateTime(timezone=True), nullable=False)
    latest_total = Column(Integer, nullable=False)
    latest_failures = Column(Integer, nullable=False)
    latest_failure_rate = Column(Float, nullable=False)

    actions = relationship("AnomalyEventAction", back_populates="event", cascade="all, delete-orphan")
    rule = relationship("AnomalyRule", back_populates="events")


class AnomalyWindowResult(Base):
    """每个规则版本在每个业务时间窗口上的最新统计；confirmed 后冻结。"""
    __tablename__ = "anomaly_window_results"
    __table_args__ = (
        UniqueConstraint("rule_id", "rule_revision", "window_start", name="uq_anomaly_window"),
    )

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(Integer, ForeignKey("anomaly_rules.id"), nullable=False, index=True)
    rule_revision = Column(Integer, nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    window_end = Column(DateTime(timezone=True), nullable=False)
    total = Column(Integer, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)
    failure_rate = Column(Float, nullable=True)
    signal = Column(String(20), nullable=True)
    confirmed = Column(Boolean, nullable=False, default=False, index=True)
    confirmed_by = Column(String(100), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)


class AnomalyEventAction(Base):
    """事件上的人工/自动操作流水：确认、忽略、恢复、重算等都保留操作者与依据。"""
    __tablename__ = "anomaly_event_actions"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("anomaly_events.id"), nullable=False, index=True)
    action = Column(String(30), nullable=False, index=True)
    operator = Column(String(100), nullable=False)
    reason = Column(Text, nullable=True)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    event = relationship("AnomalyEvent", back_populates="actions")
