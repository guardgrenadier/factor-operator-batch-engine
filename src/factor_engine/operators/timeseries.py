"""时序与 step 轴算子：日期滞后、滚动窗口与日内 step 统计。"""

from __future__ import annotations

import bottleneck as bn
import numpy as np
from numba import njit


def delay(x, periods=1, axis=0):
    """沿指定轴滞后数组。"""
    return x if periods == 0 else _delay_kernel(x, periods, axis)


def ffill(x, axis=0, limit=None):
    """沿指定轴对 nan 做前向填充。"""
    if limit == 0:
        return x
    return bn.push(x, n=x.shape[axis] if limit is None else limit, axis=axis)


def ts_ffill(x, limit=None):
    """沿日期轴对 nan 做前向填充。"""
    return ffill(x, axis=0, limit=limit)


def ts_mean(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动均值。"""
    return _rolling_move(x, window, bn.move_mean, min_periods=min_periods, axis=axis)


def ts_sum(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动求和。"""
    return _rolling_move(x, window, bn.move_sum, min_periods=min_periods, axis=axis)


def ts_std(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动标准差。"""
    return _rolling_move(
        x, window, bn.move_std, min_periods=min_periods, axis=axis, ddof=0
    )


def ts_min(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动最小值。"""
    return _rolling_move(x, window, bn.move_min, min_periods=min_periods, axis=axis)


def ts_max(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动最大值。"""
    return _rolling_move(x, window, bn.move_max, min_periods=min_periods, axis=axis)


def _rolling_move(x, window, mover, *, min_periods=None, axis=0, **kwargs):
    """用 bottleneck 实现滚动窗口聚合。"""
    min_count = window if min_periods is None else min_periods
    axis_len = x.shape[axis]
    # 轴长不足窗口时先在左侧补 nan 滚动，再裁回原长度保持对齐。
    if axis_len < window:
        pad_width = [(0, 0)] * x.ndim
        pad_width[axis] = (window - axis_len, 0)
        padded = np.pad(x, pad_width, mode="constant", constant_values=np.nan)
        moved = mover(padded, window=window, min_count=min_count, axis=axis, **kwargs)
        tail = [slice(None)] * moved.ndim
        tail[axis] = slice(-axis_len, None)
        return moved[tuple(tail)]
    return mover(x, window=window, min_count=min_count, axis=axis, **kwargs)


def get_step(x, step=0):
    """选取单个 step 并保留 step 维。"""
    return x[:, :, step : step + 1]


def slice_step(x, start=None, end=None):
    """切片选择 step 区间。"""
    return x[:, :, slice(start, end)]


def broadcast_cs(x):
    """将每个截面的单一有限统计值广播到全部资产。"""
    out = np.empty_like(x)
    # 每个 date 和 step 只允许资产间唯一一致的有限值。
    for t in range(x.shape[0]):
        for s in range(x.shape[2]):
            finite = x[t, :, s][np.isfinite(x[t, :, s])]
            value = np.nan if not len(finite) else finite[0]
            if len(finite) and not np.allclose(finite, value):
                raise ValueError(
                    "broadcast_cs requires one consistent finite value per date and step"
                )
            out[t, :, s] = value
    return out


def step_mean(x):
    """沿 step 维计算均值并输出单 step。"""
    return _step_reduce_kernel(x, 0)


def step_sum(x):
    """沿 step 维求和并输出单 step。"""
    return _step_reduce_kernel(x, 1)


def step_std(x):
    """沿 step 维计算标准差并输出单 step。"""
    return _step_reduce_kernel(x, 2)


def step_max(x):
    """沿 step 维计算最大值并输出单 step。"""
    return np.nanmax(x, axis=2, keepdims=True)


def step_min(x):
    """沿 step 维计算最小值并输出单 step。"""
    return np.nanmin(x, axis=2, keepdims=True)


def step_kurtosis(x):
    """沿 step 维计算有限样本偏差修正的超额峰度。"""
    return _step_kurtosis_kernel(x)


def step_first(x):
    """选取第一个 step。"""
    return x[:, :, :1]


def step_last(x):
    """选取最后一个 step。"""
    return x[:, :, -1:]


def step_corr(x, y):
    """沿 step 维计算两个数组的相关系数。"""
    return _step_corr_kernel(x, y)


def step_delay(x, periods=1):
    """沿 step 维滞后。"""
    return delay(x, periods=periods, axis=2)


def step_diff(x, periods=1):
    """沿 step 维计算差分。"""
    return _step_change_kernel(x, periods, False)


def step_pct_change(x, periods=1):
    """沿 step 维计算百分比变化。"""
    return _step_change_kernel(x, periods, True)


def intraday_flat_mean(x, window_days):
    """对过去多天所有 step 展平后计算均值。"""
    return _intraday_flat_kernel(x, window_days, 0)


def intraday_flat_std(x, window_days):
    """对过去多天所有 step 展平后计算标准差。"""
    return _intraday_flat_kernel(x, window_days, 1)


def intraday_by_step_mean(x, window_days):
    """对过去多天同一 step 计算均值。"""
    return _intraday_by_step_kernel(x, window_days, 0)


def intraday_by_step_std(x, window_days):
    """对过去多天同一 step 计算标准差。"""
    return _intraday_by_step_kernel(x, window_days, 1)


def ffill_to_finer_steps(x, step_index):
    """按当日步长索引把粗频率值前向填充到细频率。"""
    return x[:, :, step_index]


def align_frequency(x, target_freq=None, method=None, *, step_index=None):
    """按 Compiler 生成的 step 索引将粗频数据对齐到细频。"""
    return x[:, :, np.asarray(step_index, dtype=np.intp)]


def resample(x, target_freq=None, method=None, *, source_freq=None, boundaries=None):
    """按 Compiler 生成的连续 step 边界聚合。"""
    return _resample_kernel(x, np.asarray(boundaries), method)


@njit(cache=True)
def _delay_kernel(arr, periods, axis):
    """沿指定轴平移数组索引，越界位置输出 Missing。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 沿指定轴平移索引，移出的位置输出 Missing。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            for s in range(arr.shape[2]):
                source_t = t - periods if axis == 0 else t
                source_n = n - periods if axis == 1 else n
                source_s = s - periods if axis == 2 else s
                if (
                    source_t < 0
                    or source_n < 0
                    or source_s < 0
                    or source_t >= arr.shape[0]
                    or source_n >= arr.shape[1]
                    or source_s >= arr.shape[2]
                ):
                    out[t, n, s] = np.nan
                else:
                    out[t, n, s] = arr[source_t, source_n, source_s]
    return out


@njit(cache=True)
def _step_reduce_kernel(arr, mode):
    """沿 step 维归约为单 step，mode 选择均值、和或标准差。"""
    out = np.empty((arr.shape[0], arr.shape[1], 1), dtype=np.float64)
    # 先累计有限值的计数与平方和，再按 mode 输出统计值。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            count = 0
            total = 0.0
            total_sq = 0.0
            for s in range(arr.shape[2]):
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    total += value
                    total_sq += value * value
            if mode == 1:
                out[t, n, 0] = total
            elif count == 0:
                out[t, n, 0] = np.nan
            elif mode == 0:
                out[t, n, 0] = total / count
            else:
                variance = _population_variance(total, total_sq, count)
                out[t, n, 0] = np.sqrt(variance) if variance >= 0.0 else np.nan
    return out


@njit(cache=True)
def _step_kurtosis_kernel(arr):
    """沿 step 维增量计算偏差修正的超额峰度。"""
    out = np.empty((arr.shape[0], arr.shape[1], 1), dtype=np.float64)
    # 单次扫描增量更新各阶中心矩，样本不足 4 个时输出 Missing。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            count = 0
            mean = 0.0
            m2 = 0.0
            m3 = 0.0
            m4 = 0.0
            for s in range(arr.shape[2]):
                value = arr[t, n, s]
                if np.isfinite(value):
                    previous_count = count
                    count += 1
                    delta = value - mean
                    delta_n = delta / count
                    delta_n_sq = delta_n * delta_n
                    term = delta * delta_n * previous_count
                    m4 += (
                        term * delta_n_sq * (count * count - 3 * count + 3)
                        + 6.0 * delta_n_sq * m2
                        - 4.0 * delta_n * m3
                    )
                    m3 += term * delta_n * (count - 2) - 3.0 * delta_n * m2
                    m2 += term
                    mean += delta_n
            if count < 4:
                out[t, n, 0] = np.nan
                continue
            if m2 == 0.0:
                out[t, n, 0] = np.nan
                continue
            coefficient1 = (
                count * (count + 1) * (count - 1) / ((count - 2) * (count - 3))
            )
            coefficient2 = 3 * (count - 1) * (count - 1) / ((count - 2) * (count - 3))
            out[t, n, 0] = coefficient1 * m4 / (m2 * m2) - coefficient2
    return out


@njit(cache=True)
def _step_corr_kernel(left, right):
    """沿 step 维累计矩并输出两个数组的 Pearson 相关系数。"""
    t_count = max(left.shape[0], right.shape[0])
    n_count = max(left.shape[1], right.shape[1])
    s_count = max(left.shape[2], right.shape[2])
    out = np.empty((t_count, n_count, 1), dtype=np.float64)
    # 逐资产累计成对矩，要求两侧方差为正才输出相关系数。
    for t in range(t_count):
        for n in range(n_count):
            count = 0
            sum_x = 0.0
            sum_y = 0.0
            sum_xx = 0.0
            sum_yy = 0.0
            sum_xy = 0.0
            for s in range(s_count):
                x = left[
                    0 if left.shape[0] == 1 else t,
                    0 if left.shape[1] == 1 else n,
                    0 if left.shape[2] == 1 else s,
                ]
                y = right[
                    0 if right.shape[0] == 1 else t,
                    0 if right.shape[1] == 1 else n,
                    0 if right.shape[2] == 1 else s,
                ]
                if not np.isfinite(x) or not np.isfinite(y):
                    continue
                count += 1
                sum_x += x
                sum_y += y
                sum_xx += x * x
                sum_yy += y * y
                sum_xy += x * y
            if count < 2:
                out[t, n, 0] = np.nan
                continue
            mean_x = sum_x / count
            mean_y = sum_y / count
            variance_x = sum_xx / count - mean_x * mean_x
            variance_y = sum_yy / count - mean_y * mean_y
            covariance = sum_xy / count - mean_x * mean_y
            out[t, n, 0] = (
                covariance / np.sqrt(variance_x * variance_y)
                if variance_x > 0.0 and variance_y > 0.0
                else np.nan
            )
    return out


@njit(cache=True)
def _step_change_kernel(arr, periods, percentage):
    """沿 step 维计算差分或相对 periods 前值的百分比变化。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 前 periods 个位置输出 Missing，其余与前值比较。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            for s in range(arr.shape[2]):
                if s < periods:
                    out[t, n, s] = np.nan
                    continue
                current = arr[t, n, s]
                previous = arr[t, n, s - periods]
                value = (
                    (current - previous) / previous
                    if percentage and previous != 0.0
                    else current - previous
                )
                out[t, n, s] = value if np.isfinite(value) else np.nan
    return out


@njit(cache=True)
def _intraday_flat_kernel(arr, window, mode):
    """把过去 window 天所有 step 展平后做滑动窗口统计。"""
    out = np.empty((arr.shape[0], arr.shape[1], 1), dtype=np.float64)
    counts = np.zeros(arr.shape[1], dtype=np.int64)
    totals = np.zeros(arr.shape[1], dtype=np.float64)
    totals_sq = np.zeros(arr.shape[1], dtype=np.float64)
    # 逐日加入新值并移除过期值，维护每个资产的窗口矩。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            for s in range(arr.shape[2]):
                value = arr[t, n, s]
                if np.isfinite(value):
                    counts[n] += 1
                    totals[n] += value
                    totals_sq[n] += value * value
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        counts[n] -= 1
                        totals[n] -= expired
                        totals_sq[n] -= expired * expired
            if t < window - 1 or counts[n] == 0:
                out[t, n, 0] = np.nan
            else:
                mean = totals[n] / counts[n]
                variance = _population_variance(totals[n], totals_sq[n], counts[n])
                out[t, n, 0] = (
                    mean
                    if mode == 0
                    else np.sqrt(variance)
                    if variance >= 0.0
                    else np.nan
                )
    return out


@njit(cache=True)
def _intraday_by_step_kernel(arr, window, mode):
    """对过去 window 天同一 step 位置分别做滑动窗口统计。"""
    out = np.empty(arr.shape, dtype=np.float64)
    counts = np.zeros((arr.shape[1], arr.shape[2]), dtype=np.int64)
    totals = np.zeros((arr.shape[1], arr.shape[2]), dtype=np.float64)
    totals_sq = np.zeros((arr.shape[1], arr.shape[2]), dtype=np.float64)
    # 逐日为每个资产和 step 独立维护窗口矩。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            for s in range(arr.shape[2]):
                value = arr[t, n, s]
                if np.isfinite(value):
                    counts[n, s] += 1
                    totals[n, s] += value
                    totals_sq[n, s] += value * value
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        counts[n, s] -= 1
                        totals[n, s] -= expired
                        totals_sq[n, s] -= expired * expired
                if t < window - 1 or counts[n, s] == 0:
                    out[t, n, s] = np.nan
                else:
                    mean = totals[n, s] / counts[n, s]
                    variance = _population_variance(
                        totals[n, s], totals_sq[n, s], counts[n, s]
                    )
                    out[t, n, s] = (
                        mean
                        if mode == 0
                        else np.sqrt(variance)
                        if variance >= 0.0
                        else np.nan
                    )
    return out


@njit(cache=True)
def _resample_kernel(arr, boundaries, mode):
    """按边界把 step 区间聚合为粗频 step。"""
    out = np.empty((arr.shape[0], arr.shape[1], boundaries.shape[0]), dtype=np.float64)
    # 每个区间直接取末值或先累计矩再归约。
    for t in range(arr.shape[0]):
        for n in range(arr.shape[1]):
            for group in range(boundaries.shape[0]):
                start = boundaries[group, 0]
                stop = boundaries[group, 1]
                if mode == 3:
                    out[t, n, group] = arr[t, n, stop - 1]
                    continue
                count = 0
                total = 0.0
                total_sq = 0.0
                for s in range(start, stop):
                    value = arr[t, n, s]
                    if np.isfinite(value):
                        count += 1
                        total += value
                        total_sq += value * value
                if mode == 1:
                    out[t, n, group] = total
                elif count == 0:
                    out[t, n, group] = np.nan
                elif mode == 0:
                    out[t, n, group] = total / count
                else:
                    variance = _population_variance(total, total_sq, count)
                    out[t, n, group] = np.sqrt(variance) if variance >= 0.0 else np.nan
    return out


@njit(cache=True, inline="always")
def _population_variance(total, total_sq, denominator):
    """由累计矩计算总体方差，并消除浮点舍入产生的微小负值。"""
    mean = total / denominator
    variance = total_sq / denominator - mean * mean
    scale = max(1.0, abs(total_sq / denominator), mean * mean)
    return 0.0 if abs(variance) <= 1e-12 * scale else variance
