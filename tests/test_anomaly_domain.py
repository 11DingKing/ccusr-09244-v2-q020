"""纯领域异常监测引擎测试：窗口边界、低样本、迟滞抑制。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.anomaly import (
    AnomalyConfigError,
    AnomalyRuleConfig,
    BREACH,
    INCONCLUSIVE,
    NEUTRAL,
    RECOVERY,
    SampleRow,
    WindowSample,
    align_grid_start,
    bucket_samples,
    ceil_grid_start,
    simulate,
    window_starts,
)

UTC = timezone.utc
T0 = datetime(2025, 1, 1, tzinfo=UTC)


def cfg(**overrides) -> AnomalyRuleConfig:
    base = dict(
        window=timedelta(hours=1),
        step=timedelta(hours=1),
        minimum_sample_count=5,
        trigger_threshold=0.5,
        recovery_threshold=0.2,
    )
    base.update(overrides)
    return AnomalyRuleConfig(**base)


def make_window(start: datetime, total: int, failures: int) -> WindowSample:
    return WindowSample(start=start, end=start + timedelta(hours=1), total=total, failures=failures)


def test_window_boundaries_are_half_open_and_continuous():
    starts = window_starts(cfg(), T0, T0 + timedelta(hours=3))
    assert starts == [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]

    rows = [
        SampleRow(T0, False),                                  # 起点边界计入
        SampleRow(T0 + timedelta(minutes=59, seconds=59), True),
        SampleRow(T0 + timedelta(hours=1), False),             # 终点边界计入下一窗
        SampleRow(T0 + timedelta(hours=3) - timedelta(seconds=1), True),
    ]
    windows = bucket_samples(rows, starts, cfg())
    assert [w.total for w in windows] == [2, 1, 1]
    assert [w.failures for w in windows] == [1, 1, 0]


def test_window_must_be_fully_inside_range():
    # 只有 90 分钟，窗宽 60 分钟：只产出一个完整窗口，半截窗口不产出
    starts = window_starts(cfg(), T0, T0 + timedelta(minutes=90))
    assert starts == [T0]


def test_overlapping_windows_are_continuous():
    starts = window_starts(
        cfg(window=timedelta(hours=2), step=timedelta(hours=1)),
        T0,
        T0 + timedelta(hours=4),
    )
    assert starts == [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]


def test_grid_alignment_is_stable_across_revisions():
    rev_start = datetime(2025, 1, 1, 0, 23, tzinfo=UTC)
    first = window_starts(cfg(step=timedelta(hours=1)), rev_start, datetime(2025, 1, 2, tzinfo=UTC))
    again = window_starts(cfg(step=timedelta(hours=1)), rev_start, datetime(2025, 1, 3, tzinfo=UTC))
    # 向上对齐到整点，扩展评估时间不会改变已有窗口边界
    assert first[0] == datetime(2025, 1, 1, 1, tzinfo=UTC)
    assert again[: len(first)] == first
    assert align_grid_start(rev_start, timedelta(hours=1)) == T0
    assert ceil_grid_start(rev_start, timedelta(hours=1)) == datetime(2025, 1, 1, 1, tzinfo=UTC)


def test_low_sample_is_inconclusive_and_does_not_trigger_or_recover():
    sample = make_window(T0, total=4, failures=4)  # 失败率 100% 但低于最低样本量 5
    assert sample.signal(cfg()) == INCONCLUSIVE
    episodes = simulate([sample], cfg())
    assert episodes == []


def test_threshold_boundary_is_inclusive():
    # 失败率恰好 0.5 触发；恰好 0.2 恢复
    assert make_window(T0, 10, 5).signal(cfg()) == BREACH
    assert make_window(T0, 10, 2).signal(cfg()) == RECOVERY
    assert make_window(T0, 10, 4).signal(cfg()) == NEUTRAL


def test_hysteresis_band_does_not_flap_around_trigger_threshold():
    # 要求连续 2 个窗口越限才触发：0.5（越限）/0.4（迟滞带内）交替时，
    # 迟滞带窗口反复清零连击，阈值附近波动永远不触发。
    config = cfg(trigger_consecutive_windows=2)
    sequence = []
    for i in range(10):
        rate = 0.5 if i % 2 == 0 else 0.4
        window = make_window(T0 + timedelta(hours=i), 10, round(10 * rate))
        sequence.append(window)
    assert simulate(sequence, config) == []

    # 补上两个连续越限窗口后才触发（迟滞带窗口不能算作连续越限）
    sequence.append(make_window(T0 + timedelta(hours=10), 10, 8))
    sequence.append(make_window(T0 + timedelta(hours=11), 10, 8))
    episodes = simulate(sequence, config)
    assert len(episodes) == 1
    assert episodes[0].trigger_index == 11
    assert episodes[0].recovery_index is None


def test_consecutive_breach_windows_required():
    config = cfg(trigger_consecutive_windows=3)
    sequence = [
        make_window(T0, 10, 9), make_window(T0 + timedelta(hours=1), 10, 3),
        make_window(T0 + timedelta(hours=2), 10, 9), make_window(T0 + timedelta(hours=3), 10, 9),
    ]
    assert simulate(sequence, config) == []
    sequence.append(make_window(T0 + timedelta(hours=4), 10, 9))
    episodes = simulate(sequence, config)
    assert len(episodes) == 1 and episodes[0].trigger_index == 4


def test_recovery_requires_consecutive_recovery_windows():
    config = cfg(recovery_consecutive_windows=2)
    sequence = [
        make_window(T0, 10, 9),  # 触发
        make_window(T0 + timedelta(hours=1), 10, 1),
        make_window(T0 + timedelta(hours=2), 10, 6),  # 打断恢复连击，仍在异常中
        make_window(T0 + timedelta(hours=3), 10, 1),
        make_window(T0 + timedelta(hours=4), 10, 1),  # 连续两个恢复窗口
    ]
    episodes = simulate(sequence, config)
    assert len(episodes) == 1
    assert episodes[0].trigger_index == 0
    assert episodes[0].recovery_index == 4


def test_low_sample_during_incident_does_not_fake_recovery():
    sequence = [
        make_window(T0, 10, 9),                      # 触发
        make_window(T0 + timedelta(hours=1), 0, 0),  # 样本抽空
        make_window(T0 + timedelta(hours=2), 10, 1),
    ]
    episodes = simulate(sequence, cfg(recovery_consecutive_windows=1))
    assert len(episodes) == 1
    assert episodes[0].recovery_index == 2


def test_invalid_configs():
    with pytest.raises(AnomalyConfigError):
        cfg(step=timedelta(hours=2)).validate()  # 步长大于窗宽会产生空档
    with pytest.raises(AnomalyConfigError):
        cfg(recovery_threshold=0.5).validate()  # 恢复阈值必须严格低于触发阈值
    with pytest.raises(AnomalyConfigError):
        AnomalyRuleConfig(
            window=timedelta(hours=1), step=timedelta(hours=1),
            minimum_sample_count=0, trigger_threshold=0.5, recovery_threshold=0.2,
        ).validate()
