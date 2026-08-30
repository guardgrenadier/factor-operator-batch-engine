"""覆盖算子内核零拷贝视图、数值正确性与不可变性的优化测试。"""

from __future__ import annotations

import numpy as np

from factor_engine.operators.alignment import broadcast_to_steps, lookup_by_col
from factor_engine.operators.cross_section import (
    cs_mean,
    cs_std,
    cs_sum,
    cs_zscore,
    group_mean,
    neutralize,
)
from factor_engine.operators.timeseries import (
    delay,
    get_step,
    intraday_by_step_mean,
    intraday_by_step_std,
    intraday_flat_mean,
    intraday_flat_std,
    slice_step,
    step_corr,
    step_kurtosis,
)


def test_identity_and_selection_operators_return_views() -> None:
    """验证恒等与选取算子返回共享内存的视图而非副本。"""
    values = np.arange(24, dtype=np.float64).reshape(2, 3, 4)

    assert delay(values, periods=0) is values
    for selected in (get_step(values, 1), slice_step(values, 1, 3)):
        assert np.shares_memory(values, selected)
    broadcast = broadcast_to_steps(values[:, :, :1], 4)
    assert np.shares_memory(values, broadcast)
    assert broadcast.strides[2] == 0
    assert not broadcast.flags.writeable


def test_cross_section_kernels_match_numpy_without_modifying_inputs() -> None:
    """验证截面内核结果与 NumPy 一致且不改动输入数组。"""
    values = np.array([[[1.0, 2.0], [3.0, np.nan], [5.0, 8.0], [7.0, 4.0]]])
    sample = np.array([[[1.0], [0.0], [1.0], [1.0]]])
    original = values.copy()
    masked = np.where(sample == 1.0, values, np.nan)

    np.testing.assert_allclose(
        cs_mean(values, sample), np.nanmean(masked, axis=1, keepdims=True)
    )
    np.testing.assert_allclose(
        cs_sum(values, sample), np.nansum(masked, axis=1, keepdims=True)
    )
    np.testing.assert_allclose(
        cs_std(values, sample), np.nanstd(masked, axis=1, keepdims=True)
    )
    mean = np.nanmean(masked, axis=1, keepdims=True)
    std = np.nanstd(masked, axis=1, keepdims=True)
    np.testing.assert_allclose(cs_zscore(values, sample), (values - mean) / std)
    np.testing.assert_array_equal(values, original)


def test_neutralize_uses_singleton_exposure_without_materializing_it() -> None:
    """验证 neutralize 直接使用单 step 暴露视图而不实体化。"""
    values = np.arange(24, dtype=np.float64).reshape(2, 4, 3)
    exposure = np.array([[[1.0], [2.0], [4.0], [8.0]]] * 2)
    expected = np.empty_like(values)
    for t in range(values.shape[0]):
        design = np.column_stack((np.ones(values.shape[1]), exposure[t, :, 0]))
        for step in range(values.shape[2]):
            beta, *_ = np.linalg.lstsq(design, values[t, :, step], rcond=None)
            expected[t, :, step] = values[t, :, step] - design @ beta

    result = neutralize(values, exposure)

    np.testing.assert_allclose(result, expected, atol=1e-12)
    assert exposure.shape == (2, 4, 1)


def test_step_kernels_accept_sliced_and_singleton_views() -> None:
    """验证 step 内核能正确处理非连续切片与单 step 视图。"""
    base = np.arange(2 * 3 * 10, dtype=np.float64).reshape(2, 3, 10)
    values = base[:, :, ::2]
    singleton = values[:, :, :1]
    count = values.shape[2]
    mean = np.mean(values, axis=2, keepdims=True)
    centered = values - mean
    m2 = np.sum(centered**2, axis=2, keepdims=True)
    m4 = np.sum(centered**4, axis=2, keepdims=True)
    expected_kurtosis = (
        count * (count + 1) * (count - 1) / ((count - 2) * (count - 3))
    ) * m4 / (m2**2) - 3 * (count - 1) ** 2 / ((count - 2) * (count - 3))

    np.testing.assert_allclose(step_kurtosis(values), expected_kurtosis)
    assert np.isnan(step_corr(values, singleton)).all()
    assert not values.flags.c_contiguous


def test_intraday_rolling_state_matches_naive_windows() -> None:
    """验证日内滚动算子结果与逐窗口朴素计算一致。"""
    values = np.arange(6 * 3 * 4, dtype=np.float64).reshape(6, 3, 4)
    values[2, 1, 3] = np.nan
    window = 3

    for operation, reducer, flattened in (
        (intraday_flat_mean, np.nanmean, True),
        (intraday_flat_std, np.nanstd, True),
        (intraday_by_step_mean, np.nanmean, False),
        (intraday_by_step_std, np.nanstd, False),
    ):
        expected = np.full(
            (6, 3, 1) if flattened else values.shape, np.nan, dtype=np.float64
        )
        for date in range(window - 1, values.shape[0]):
            chunk = values[date - window + 1 : date + 1]
            if flattened:
                expected[date, :, 0] = reducer(chunk, axis=(0, 2))
            else:
                expected[date] = reducer(chunk, axis=0)
        np.testing.assert_allclose(operation(values, window), expected, atol=1e-12)


def test_code_consumers_keep_float64_inputs_immutable() -> None:
    """验证分组与按列查找算子不改动其 float64 输入数组。"""
    values = np.arange(12, dtype=np.float64).reshape(1, 4, 3)
    groups = np.array([[[1.0], [1.0], [2.0], [2.0]]])
    columns = np.array([[[3.0], [1.0], [np.nan]]])
    groups_before = groups.copy()
    columns_before = columns.copy()

    np.testing.assert_allclose(
        group_mean(values, groups),
        [[[1.5, 2.5, 3.5], [1.5, 2.5, 3.5], [7.5, 8.5, 9.5], [7.5, 8.5, 9.5]]],
    )
    projected = lookup_by_col(values, columns)
    np.testing.assert_allclose(
        projected, [[[9.0, 10.0, 11.0], [3.0, 4.0, 5.0], [np.nan] * 3]], equal_nan=True
    )
    np.testing.assert_array_equal(groups, groups_before)
    np.testing.assert_array_equal(columns, columns_before)
