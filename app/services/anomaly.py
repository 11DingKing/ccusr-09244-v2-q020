"""失败率异常监测的纯领域逻辑。

本模块不访问数据库，只描述三件事：

1. 规则如何按业务时间切出连续、不跳点的窗口；
2. 单窗口样本如何判定为越限 / 恢复 / 证据不足；
3. 窗口序列如何驱动“正常 → 异常 → 恢复”的状态机，
   并通过迟滞阈值与连续窗口计数抑制阈值附近的反复开关。

所有函数都是确定性的，持久化层可以反复重放同一段窗口序列，
得到完全一致的“异常片段（episode）”，这是幂等重算与重启恢复的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, NamedTuple, Sequence


class AnomalyConfigError(ValueError):
    """监测规则参数不合法。"""


# 单窗口的判定信号
BREACH = "breach"            # 失败率达到触发阈值
RECOVERY = "recovery"        # 失败率回落到恢复阈值
NEUTRAL = "neutral"          # 位于迟滞带内，维持现状
INCONCLUSIVE = "inconclusive"  # 样本不足，不改变任何状态

EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class AnomalyRuleConfig:
    """规则中与判定有关的参数（与 ORM 解耦，便于单测）。"""

    window: timedelta
    step: timedelta
    minimum_sample_count: int
    trigger_threshold: float
    recovery_threshold: float
    trigger_consecutive_windows: int = 1
    recovery_consecutive_windows: int = 1

    def validate(self) -> "AnomalyRuleConfig":
        if self.window <= timedelta(0) or self.step <= timedelta(0):
            raise AnomalyConfigError("窗口宽度与步长必须为正")
        if self.step > self.window:
            raise AnomalyConfigError("步长不能大于窗口宽度，否则连续窗口之间会出现监测空档")
        if self.minimum_sample_count < 1:
            raise AnomalyConfigError("最低样本量至少为 1")
        if not 0.0 < self.trigger_threshold <= 1.0:
            raise AnomalyConfigError("触发阈值必须位于 0 到 1 之间")
        if not 0.0 <= self.recovery_threshold < 1.0:
            raise AnomalyConfigError("恢复阈值必须位于 0 到 1 之间")
        if self.recovery_threshold >= self.trigger_threshold:
            raise AnomalyConfigError("恢复阈值必须严格小于触发阈值以形成迟滞带")
        if self.trigger_consecutive_windows < 1 or self.recovery_consecutive_windows < 1:
            raise AnomalyConfigError("连续窗口计数至少为 1")
        return self


@dataclass(frozen=True)
class SampleRow:
    """一条已标注作业在业务时间上的样本。"""

    happened_at: datetime
    is_success: bool

    def normalized(self) -> "SampleRow":
        if self.happened_at.tzinfo is None:
            raise AnomalyConfigError("业务时间必须带时区")
        return SampleRow(self.happened_at.astimezone(timezone.utc), self.is_success)


@dataclass(frozen=True)
class WindowSample:
    """一个连续窗口内的样本统计。"""

    start: datetime
    end: datetime
    total: int
    failures: int

    @property
    def failure_rate(self) -> float | None:
        return self.failures / self.total if self.total else None

    def signal(self, config: AnomalyRuleConfig) -> str:
        rate = self.failure_rate
        if rate is None or self.total < config.minimum_sample_count:
            return INCONCLUSIVE
        if rate >= config.trigger_threshold:
            return BREACH
        if rate <= config.recovery_threshold:
            return RECOVERY
        return NEUTRAL


class Episode(NamedTuple):
    """状态机重放出的一段完整异常：在 trigger_index 触发，
    在 recovery_index 恢复（None 表示截至当前窗口仍在持续）。"""

    trigger_index: int
    recovery_index: int | None


def align_grid_start(anchor: datetime, step: timedelta) -> datetime:
    """把任意锚点向下对齐到以 2000-01-01 为起点的步长网格，
    保证同一规则重算出的窗口边界永远一致。"""
    if anchor.tzinfo is None:
        raise AnomalyConfigError("锚点时间必须带时区")
    anchor_utc = anchor.astimezone(timezone.utc)
    step_seconds = int(step.total_seconds())
    elapsed = int((anchor_utc - EPOCH).total_seconds())
    aligned = elapsed - (elapsed % step_seconds)
    return EPOCH + timedelta(seconds=aligned)


def ceil_grid_start(anchor: datetime, step: timedelta) -> datetime:
    """向上对齐到步长网格：得到不早于锚点的第一个网格点。"""
    lower = align_grid_start(anchor, step)
    if lower == anchor.astimezone(timezone.utc):
        return lower
    return lower + step


def window_starts(
    config: AnomalyRuleConfig,
    range_start: datetime,
    range_end: datetime,
    *,
    snap: str = "ceil",
) -> list[datetime]:
    """生成 ``[range_start, range_end)`` 内的连续窗口起点，左闭右开。

    - ``snap="ceil"``（默认）：第一个窗口起点取不早于 range_start 的网格
      点，窗口严格落在评估范围内——版本切换时用它，窗口不会跨越版本
      生效时刻；
    - ``snap="floor"``：第一个窗口起点取不晚于 range_start 的网格点；
    - 只产出**完整**落在 ``range_end`` 之前的窗口（窗口末端 <= range_end），
      所以以“当前时刻”评估时只会统计已经走完的窗口；
    - 窗口之间允许重叠（步长 < 窗宽），但起点严格连续、不跳点。
    """
    config.validate()
    if range_start.tzinfo is None or range_end.tzinfo is None:
        raise AnomalyConfigError("查询范围必须带时区")
    if snap == "ceil":
        start_bound = ceil_grid_start(range_start.astimezone(timezone.utc), config.step)
    elif snap == "floor":
        start_bound = align_grid_start(range_start.astimezone(timezone.utc), config.step)
    else:
        raise AnomalyConfigError("snap 只能是 ceil 或 floor")
    end_bound = range_end.astimezone(timezone.utc)
    if start_bound >= end_bound:
        return []
    starts: list[datetime] = []
    cursor = start_bound
    while cursor + config.window <= end_bound:
        starts.append(cursor)
        cursor += config.step
    return starts


def bucket_samples(
    rows: Iterable[SampleRow],
    starts: Sequence[datetime],
    config: AnomalyRuleConfig,
) -> list[WindowSample]:
    """把样本按业务时间分进给定的左闭右开窗口。

    边界约定：作业时间恰好等于窗口起点计入本窗口；恰好等于窗口终点
    则计入下一窗口。没有任何样本的窗口也会产出（total=0，证据不足），
    保证窗口序列连续。
    """
    ordered = sorted((row.normalized() for row in rows), key=lambda row: row.happened_at)
    samples: list[WindowSample] = []
    for start in starts:
        start_utc = start.astimezone(timezone.utc)
        end_utc = start_utc + config.window
        members = [row for row in ordered if start_utc <= row.happened_at < end_utc]
        samples.append(
            WindowSample(
                start=start_utc,
                end=end_utc,
                total=len(members),
                failures=sum(1 for row in members if not row.is_success),
            )
        )
    return samples


def simulate(samples: Sequence[WindowSample], config: AnomalyRuleConfig) -> list[Episode]:
    """对连续窗口序列重放状态机，返回所有异常片段。

    抑制规则：
    - 触发需连续 ``trigger_consecutive_windows`` 个越限窗口；迟滞带内的
      NEUTRAL 窗口和证据不足窗口都会清零越限连击，因此阈值附近来回波动、
      或中间夹着低样本窗口都不会触发；
    - 一旦进入异常，只有连续 ``recovery_consecutive_windows`` 个恢复窗口
      才能结束片段；迟滞带内波动或低样本窗口维持异常、不产生新片段；
    - 证据不足（低样本）窗口不会触发恢复，避免样本被抽空后“假恢复”。
    """
    config.validate()
    episodes: list[Episode] = []
    inside = False
    breach_run = 0
    recovery_run = 0
    trigger_index: int | None = None

    for index, sample in enumerate(samples):
        signal = sample.signal(config)
        if not inside:
            if signal == BREACH:
                breach_run += 1
                if breach_run >= config.trigger_consecutive_windows:
                    inside = True
                    trigger_index = index
                    breach_run = 0
                    recovery_run = 0
            else:
                # NEUTRAL / RECOVERY / INCONCLUSIVE 都打断越限连续性
                breach_run = 0
        else:
            if signal == RECOVERY:
                recovery_run += 1
                if recovery_run >= config.recovery_consecutive_windows:
                    episodes.append(Episode(trigger_index, index))
                    inside = False
                    trigger_index = None
                    recovery_run = 0
                    breach_run = 0
            else:
                recovery_run = 0
            # 异常期间的 BREACH/NEUTRAL/INCONCLUSIVE 都不会另开新片段

    if inside and trigger_index is not None:
        episodes.append(Episode(trigger_index, None))
    return episodes
