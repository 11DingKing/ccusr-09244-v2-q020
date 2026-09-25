"""失败率异常监测的领域规则：连续窗口、迟滞状态机与可解释快照。

纯 Python 实现，不依赖数据库，便于独立测试：
- 窗口按作业业务时间连续切分，以 Unix 纪元对齐，落窗规则为 [start, end)；
- 触发要求最近连续 N 个可评估窗口失败率不低于触发阈值；
- 恢复要求最新可评估窗口失败率回落至恢复阈值或以下；
- 触发阈值与恢复阈值之间为迟滞区，窗口落入其中既不触发也不恢复，
  避免同一异常在阈值附近波动时反复开关；
- 样本量不足的窗口不可评估，既中断连续计数，也不触发恢复；
- 已确认、已忽略或已恢复事件覆盖的窗口区间被锁定，迟到数据不再改写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Sequence


class AnomalyError(ValueError):
    """异常监测规则或操作不合法。"""


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class EventStatus(str, Enum):
    """异常事件的生命周期状态。"""

    OPEN = "open"  # 已打开，等待运营处理
    ACKNOWLEDGED = "acknowledged"  # 已确认，问题已知悉
    SUPPRESSED = "suppressed"  # 已忽略，视为无需处理
    RESOLVED = "resolved"  # 已恢复（系统自动或人工）


ACTIVE_STATUSES = (EventStatus.OPEN.value, EventStatus.ACKNOWLEDGED.value)
TERMINAL_STATUSES = (EventStatus.SUPPRESSED.value, EventStatus.RESOLVED.value)


class EventAction(str, Enum):
    """运营对事件可执行的动作。"""

    CONFIRM = "confirm"
    IGNORE = "ignore"
    RESOLVE = "resolve"


_ACTION_TRANSITIONS = {
    EventAction.CONFIRM: (EventStatus.ACKNOWLEDGED, (EventStatus.OPEN,)),
    EventAction.IGNORE: (EventStatus.SUPPRESSED, (EventStatus.OPEN, EventStatus.ACKNOWLEDGED)),
    EventAction.RESOLVE: (EventStatus.RESOLVED, (EventStatus.OPEN, EventStatus.ACKNOWLEDGED)),
}


def apply_action(status: str, action: EventAction) -> EventStatus:
    """校验状态流转并返回目标状态；非法流转抛出 AnomalyError。"""
    target, allowed = _ACTION_TRANSITIONS[action]
    current = EventStatus(status)
    if current not in allowed:
        raise AnomalyError(f"事件处于 {current.value} 状态，不允许执行 {action.value} 操作")
    return target


@dataclass(frozen=True)
class RuleSpec:
    """异常规则某个版本的不可变判定参数。"""

    window_size: timedelta
    min_samples: int
    trigger_threshold: float
    recovery_threshold: float
    consecutive_windows: int

    def validate(self) -> "RuleSpec":
        if self.window_size <= timedelta(0):
            raise AnomalyError("窗口宽度必须为正")
        if self.min_samples < 1:
            raise AnomalyError("最低样本量至少为一")
        if not 0.0 < self.trigger_threshold <= 1.0:
            raise AnomalyError("触发阈值必须位于零到一之间")
        if not 0.0 <= self.recovery_threshold <= 1.0:
            raise AnomalyError("恢复阈值必须位于零到一之间")
        if self.recovery_threshold >= self.trigger_threshold:
            raise AnomalyError("恢复阈值必须小于触发阈值，形成迟滞区间")
        if self.consecutive_windows < 1:
            raise AnomalyError("连续触发窗口数至少为一")
        return self

    def snapshot(self) -> dict[str, object]:
        """生成可嵌入事件的阈值快照，规则变更后旧事件仍保留原判定依据。"""
        return {
            "window_size_minutes": int(self.window_size.total_seconds() // 60),
            "min_samples": self.min_samples,
            "trigger_threshold": self.trigger_threshold,
            "recovery_threshold": self.recovery_threshold,
            "consecutive_windows": self.consecutive_windows,
        }


@dataclass(frozen=True)
class WindowStat:
    """单个连续窗口内已标注样本的失败率统计。"""

    start: datetime
    end: datetime
    sample_count: int
    failure_count: int

    @property
    def failure_rate(self) -> float | None:
        if self.sample_count == 0:
            return None
        return self.failure_count / self.sample_count

    def evaluable(self, spec: RuleSpec) -> bool:
        return self.sample_count >= spec.min_samples

    def breaches(self, spec: RuleSpec) -> bool:
        rate = self.failure_rate
        return rate is not None and rate >= spec.trigger_threshold

    def recovered(self, spec: RuleSpec) -> bool:
        rate = self.failure_rate
        return rate is not None and rate <= spec.recovery_threshold


def align_down(moment: datetime, size: timedelta) -> datetime:
    """把业务时间向下对齐到窗口起点；纪元对齐保证同一宽度的窗口边界永远稳定。"""
    if moment.tzinfo is None:
        raise AnomalyError("业务时间必须带时区")
    current = moment.astimezone(timezone.utc)
    return EPOCH + (current - EPOCH) // size * size


def windows_between(start: datetime, end: datetime, size: timedelta) -> list[tuple[datetime, datetime]]:
    """生成 [start, end) 内完整闭合的连续窗口；起点必须已对齐，不足一个窗口不评估。"""
    if end.tzinfo is None:
        raise AnomalyError("截止时间必须带时区")
    aligned = align_down(start, size)
    if aligned != start.astimezone(timezone.utc):
        raise AnomalyError("评估起点未对齐窗口边界")
    final = end.astimezone(timezone.utc)
    result: list[tuple[datetime, datetime]] = []
    cursor = aligned
    while cursor + size <= final:
        result.append((cursor, cursor + size))
        cursor += size
    return result


def window_locked(
    start: datetime,
    end: datetime,
    locked_intervals: Sequence[tuple[datetime, datetime]],
) -> bool:
    """窗口与任一已确认区间相交即锁定，迟到的补录数据不再改写该窗口。"""
    return any(locked_start < end and start < locked_end for locked_start, locked_end in locked_intervals)


def tail_breach_windows(
    stats: Sequence[WindowStat],
    spec: RuleSpec,
    locked_intervals: Sequence[tuple[datetime, datetime]],
) -> list[WindowStat]:
    """从最新窗口向回收集连续超阈值窗口；低样本、回落或锁定窗口都会中断连续性。"""
    breaches: list[WindowStat] = []
    for stat in reversed(stats):
        if window_locked(stat.start, stat.end, locked_intervals):
            break
        if not stat.evaluable(spec) or not stat.breaches(spec):
            break
        breaches.append(stat)
    return breaches


def trigger_windows(
    stats: Sequence[WindowStat],
    spec: RuleSpec,
    has_active_event: bool,
    locked_intervals: Sequence[tuple[datetime, datetime]],
) -> list[WindowStat]:
    """返回应作为新事件触发依据的最近连续窗口；空列表表示不触发。

    已有活动事件时不会重复打开；触发窗口不得落入已锁定区间，
    因此同一批超阈值窗口被忽略后不会再次触发新事件。
    """
    if has_active_event:
        return []
    breaches = tail_breach_windows(stats, spec, locked_intervals)
    if len(breaches) < spec.consecutive_windows:
        return []
    return list(reversed(breaches[: spec.consecutive_windows]))


def resolve_window(stats: Sequence[WindowStat], spec: RuleSpec, has_active_event: bool) -> WindowStat | None:
    """最新可评估窗口回落至恢复阈值或以下时返回该窗口，否则不恢复。

    低样本窗口不参与判断：数据不足时保持事件现状，不妄动。
    """
    if not has_active_event:
        return None
    for stat in reversed(stats):
        if not stat.evaluable(spec):
            continue
        return stat if stat.recovered(spec) else None
    return None
