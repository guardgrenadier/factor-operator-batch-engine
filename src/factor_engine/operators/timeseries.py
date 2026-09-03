"""时序与 step 轴算子：日期滞后、滚动窗口与日内 step 统计。"""

from __future__ import annotations

import numpy as np
from numba import njit


def delay(x, periods=1, axis=0):
    """沿指定轴滞后数组。"""
    return x if periods == 0 else _delay_kernel(x, periods, axis)


def ffill(x, axis=0, limit=None):
    """沿指定轴对 nan 做前向填充，每段缺失最多填充 limit 个位置。"""
    if limit == 0:
        return x
    work = _axis0_view(x, axis)
    out = _ffill_kernel(work, work.shape[0] if limit is None else int(limit))
    return _axis0_restore(out, axis)


def ts_ffill(x, limit=None):
    """沿日期轴对 nan 做前向填充。"""
    return ffill(x, axis=0, limit=limit)


def ts_mean(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动均值。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_MEAN)


def ts_sum(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动求和。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_SUM)


def ts_std(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动总体标准差（ddof=0）。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_STD)


def ts_var(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动总体方差（ddof=0）。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_VAR)


def ts_min(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动最小值。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_MIN)


def ts_max(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动最大值。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_MAX)


def ts_median(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动中位数。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_MEDIAN)


def ts_quantile(x, window=10, quantile=0.5, min_periods=None, axis=0):
    """沿指定轴计算滚动分位数（线性插值）。"""
    work = _axis0_view(x, axis)
    out = _roll_quantile_kernel(
        work, int(window), _min_count(window, min_periods), float(quantile)
    )
    return _axis0_restore(out, axis)


def ts_argmin(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算窗口最小值距今的回溯步数（0 表示当前值最小）。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_ARGMIN)


def ts_argmax(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算窗口最大值距今的回溯步数（0 表示当前值最大）。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_ARGMAX)


def ts_rank(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算当前值在窗口内的百分位排名（0 到 1，平局取中位秩）。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_RANK)


def ts_cumprod(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算窗口内 (1 + x) 的累乘。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_CUMPROD)


def ts_max_to_min(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动最大值与最小值之差。"""
    return _roll_unary(x, window, min_periods, axis, _ROLL_RANGE)


def ts_diff(x, periods=1, axis=0):
    """沿指定轴计算与 periods 步之前的差分。"""
    return _ts_change(x, periods, axis, percentage=False)


def ts_pct_change(x, periods=1, axis=0):
    """沿指定轴计算相对 periods 步之前的百分比变化，前值为零输出 nan。"""
    return _ts_change(x, periods, axis, percentage=True)


def ts_corr(x, y, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动 Pearson 相关系数。"""
    return _roll_pair(x, y, window, min_periods, axis, _PAIR_CORR)


def ts_cov(x, y, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动样本协方差（ddof=1）。"""
    return _roll_pair(x, y, window, min_periods, axis, _PAIR_COV)


def ts_beta(x, y, window=5, min_periods=None, axis=0):
    """沿指定轴计算 y 对 x 的滚动回归斜率。"""
    return _roll_pair(x, y, window, min_periods, axis, _PAIR_BETA)


def ts_tbeta(x, window=5, min_periods=None, axis=0):
    """沿指定轴计算值对窗口内时间序号的滚动回归斜率。"""
    work = _axis0_view(x, axis)
    out = _roll_tbeta_kernel(work, int(window), _min_count(window, min_periods))
    return _axis0_restore(out, axis)


def ts_rankcorr(x, y, window=5, min_periods=None, axis=0):
    """沿指定轴计算滚动 Spearman 秩相关系数。"""
    left, right = _paired_views(x, y)
    work_x = _axis0_view(left, axis)
    work_y = _axis0_view(right, axis)
    out = _roll_rankcorr_kernel(
        work_x, work_y, int(window), _min_count(window, min_periods)
    )
    return _axis0_restore(out, axis)


def ts_split_mean(x, y, window=5, top=1, min_periods=None, axis=0):
    """沿指定轴对窗口内 y 最大的前 top 个样本计算 x 的均值。"""
    return _roll_split(x, y, window, top, min_periods, axis, _SPLIT_MEAN)


def ts_split_std(x, y, window=5, top=1, min_periods=None, axis=0):
    """沿指定轴对窗口内 y 最大的前 top 个样本计算 x 的总体标准差。"""
    return _roll_split(x, y, window, top, min_periods, axis, _SPLIT_STD)


def ts_split_corr(x, y, window=5, top=1, min_periods=None, axis=0):
    """沿指定轴对窗口内 y 最大的前 top 个样本计算 x 与 y 的相关系数。"""
    return _roll_split(x, y, window, top, min_periods, axis, _SPLIT_CORR)


def ts_ewm_mean(x, halflife=5, window=10, min_periods=None, axis=0):
    """沿指定轴按半衰期指数衰减权重计算滚动加权均值。"""
    return _roll_ewm(x, halflife, window, min_periods, axis, _EWM_MEAN)


def ts_ewm_std(x, halflife=5, window=10, min_periods=None, axis=0):
    """沿指定轴按半衰期指数衰减权重计算滚动加权总体标准差。"""
    return _roll_ewm(x, halflife, window, min_periods, axis, _EWM_STD)


# 滚动窗口统计的 mode 编码，供 numba kernel 分派使用。
_ROLL_MEAN = 0
_ROLL_SUM = 1
_ROLL_STD = 2
_ROLL_VAR = 3
_ROLL_MIN = 4
_ROLL_MAX = 5
_ROLL_MEDIAN = 6
_ROLL_ARGMIN = 7
_ROLL_ARGMAX = 8
_ROLL_RANK = 9
_ROLL_CUMPROD = 10
_ROLL_RANGE = 11
_PAIR_CORR = 0
_PAIR_COV = 1
_PAIR_BETA = 2
_SPLIT_MEAN = 0
_SPLIT_STD = 1
_SPLIT_CORR = 2
_EWM_MEAN = 0
_EWM_STD = 1


def _axis0_view(x, axis):
    """把目标计算轴移到第 0 维，供只支持日期轴语义的 kernel 复用。"""
    return np.moveaxis(x, axis, 0) if axis != 0 else x


def _axis0_restore(out, axis):
    """把第 0 维移回原始轴位置。"""
    return np.moveaxis(out, 0, axis) if axis != 0 else out


def _min_count(window, min_periods):
    """按既有契约把 min_periods=None 规范为完整窗口。"""
    return int(window if min_periods is None else min_periods)


def _paired_views(x, y):
    """把两个输入按 NumPy 广播规则对齐为只读视图。"""
    left, right = np.broadcast_arrays(
        np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    )
    return left, right


def _roll_unary(x, window, min_periods, axis, mode):
    """单输入滚动统计的公共入口，按统计量分派到滑动或重扫 kernel。"""
    work = _axis0_view(x, axis)
    size = int(window)
    min_count = _min_count(window, min_periods)
    if mode in (_ROLL_MEAN, _ROLL_SUM, _ROLL_STD, _ROLL_VAR):
        out = _roll_moment_kernel(work, size, min_count, mode)
    elif mode in (_ROLL_MIN, _ROLL_MAX, _ROLL_RANGE):
        out = _roll_minmax_kernel(work, size, min_count, mode)
    elif mode in (_ROLL_ARGMIN, _ROLL_ARGMAX):
        out = _roll_argextreme_kernel(work, size, min_count, mode)
    elif mode == _ROLL_MEDIAN:
        out = _roll_quantile_kernel(work, size, min_count, 0.5)
    else:
        out = _roll_rescan_kernel(work, size, min_count, mode)
    return _axis0_restore(out, axis)


def _ts_change(x, periods, axis, *, percentage):
    """沿指定轴计算差分或百分比变化。"""
    work = _axis0_view(x, axis)
    out = _ts_change_kernel(work, int(periods), percentage)
    return _axis0_restore(out, axis)


def _roll_pair(x, y, window, min_periods, axis, mode):
    """双输入滚动统计的公共入口。"""
    left, right = _paired_views(x, y)
    work_x = _axis0_view(left, axis)
    work_y = _axis0_view(right, axis)
    out = _roll_pair_kernel(
        work_x, work_y, int(window), _min_count(window, min_periods), mode
    )
    return _axis0_restore(out, axis)


def _roll_split(x, y, window, top, min_periods, axis, mode):
    """窗口内按第二输入截取头部样本的滚动统计入口。"""
    left, right = _paired_views(x, y)
    work_x = _axis0_view(left, axis)
    work_y = _axis0_view(right, axis)
    out = _roll_split_kernel(
        work_x, work_y, int(window), int(top), _min_count(window, min_periods), mode
    )
    return _axis0_restore(out, axis)


def _roll_ewm(x, halflife, window, min_periods, axis, mode):
    """指数衰减加权滚动统计的公共入口。"""
    work = _axis0_view(x, axis)
    out = _roll_ewm_kernel(
        work, float(halflife), int(window), _min_count(window, min_periods), mode
    )
    return _axis0_restore(out, axis)


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
def _ffill_kernel(arr, limit):
    """沿第 0 轴前向填充 nan，每段缺失序列最多填充 limit 个位置。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 缺失段从最近的有限值之后开始计数，超过 limit 的位置保持缺失。
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            last = np.nan
            gap = limit + 1
            for t in range(arr.shape[0]):
                value = arr[t, n, s]
                if not np.isfinite(value):
                    gap += 1
                    out[t, n, s] = last if gap <= limit else np.nan
                else:
                    last = value
                    gap = 0
                    out[t, n, s] = value
    return out


@njit(cache=True)
def _roll_moment_kernel(arr, window, min_count, mode):
    """沿第 0 轴滑动维护有效值计数与矩，输出滚动均值、和或总体方差/标准差。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 逐条序列滑动：加入新值、移除 window 步之前的过期值，窗口在左边界截断。
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            count = 0
            total = 0.0
            total_sq = 0.0
            for t in range(arr.shape[0]):
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    total += value
                    total_sq += value * value
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        count -= 1
                        total -= expired
                        total_sq -= expired * expired
                if count < min_count:
                    out[t, n, s] = np.nan
                elif mode == _ROLL_MEAN:
                    out[t, n, s] = total / count
                elif mode == _ROLL_SUM:
                    out[t, n, s] = total
                else:
                    variance = _population_variance(total, total_sq, count)
                    if mode == _ROLL_VAR:
                        out[t, n, s] = variance
                    else:
                        out[t, n, s] = np.sqrt(variance) if variance >= 0.0 else np.nan
    return out


@njit(cache=True)
def _roll_minmax_kernel(arr, window, min_count, mode):
    """沿第 0 轴用循环单调双端队列以均摊 O(1) 输出滚动最值或极差。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # RANGE 同时维护两条队列，MIN/MAX 只维护需要的一条。
    need_min = mode != _ROLL_MAX
    need_max = mode != _ROLL_MIN
    # 队列缓冲按值单调存放窗口内候选位置，全部序列复用同一份分配。
    min_queue = np.empty(window, dtype=np.int64)
    max_queue = np.empty(window, dtype=np.int64)
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            min_head = 0
            min_len = 0
            max_head = 0
            max_len = 0
            count = 0
            for t in range(arr.shape[0]):
                # 先让 window 步之前的位置过期出队，再插入新位置，保持长度不超过 window。
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        count -= 1
                        if min_len > 0 and min_queue[min_head] == t - window:
                            min_head += 1
                            if min_head == window:
                                min_head = 0
                            min_len -= 1
                        if max_len > 0 and max_queue[max_head] == t - window:
                            max_head += 1
                            if max_head == window:
                                max_head = 0
                            max_len -= 1
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    if need_min:
                        while min_len > 0:
                            back_pos = min_head + min_len - 1
                            if back_pos >= window:
                                back_pos -= window
                            if arr[min_queue[back_pos], n, s] < value:
                                break
                            min_len -= 1
                        write_pos = min_head + min_len
                        if write_pos >= window:
                            write_pos -= window
                        min_queue[write_pos] = t
                        min_len += 1
                    if need_max:
                        while max_len > 0:
                            back_pos = max_head + max_len - 1
                            if back_pos >= window:
                                back_pos -= window
                            if arr[max_queue[back_pos], n, s] > value:
                                break
                            max_len -= 1
                        write_pos = max_head + max_len
                        if write_pos >= window:
                            write_pos -= window
                        max_queue[write_pos] = t
                        max_len += 1
                queue_empty = (need_min and min_len == 0) or (need_max and max_len == 0)
                if count < min_count or queue_empty:
                    out[t, n, s] = np.nan
                elif mode == _ROLL_MIN:
                    out[t, n, s] = arr[min_queue[min_head], n, s]
                elif mode == _ROLL_MAX:
                    out[t, n, s] = arr[max_queue[max_head], n, s]
                else:
                    out[t, n, s] = (
                        arr[max_queue[max_head], n, s] - arr[min_queue[min_head], n, s]
                    )
    return out


@njit(cache=True)
def _roll_argextreme_kernel(arr, window, min_count, mode):
    """沿第 0 轴用循环单调队列输出窗口最值距今的回溯步数（0 表示当前）。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 队列缓冲在全部序列间复用，每条序列只重置游标。
    queue = np.empty(window, dtype=np.int64)
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            head = 0
            length = 0
            count = 0
            for t in range(arr.shape[0]):
                # 先让 window 步之前的位置过期出队，再插入新位置。
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        count -= 1
                        if length > 0 and queue[head] == t - window:
                            head += 1
                            if head == window:
                                head = 0
                            length -= 1
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    # 从队尾驱逐不优于当前值的旧位置，相等时保留最新。
                    while length > 0:
                        back_pos = head + length - 1
                        if back_pos >= window:
                            back_pos -= window
                        back = arr[queue[back_pos], n, s]
                        if mode == _ROLL_ARGMIN and back >= value:
                            length -= 1
                        elif mode == _ROLL_ARGMAX and back <= value:
                            length -= 1
                        else:
                            break
                    write_pos = head + length
                    if write_pos >= window:
                        write_pos -= window
                    queue[write_pos] = t
                    length += 1
                if count < min_count or length == 0:
                    out[t, n, s] = np.nan
                else:
                    out[t, n, s] = float(t - queue[head])
    return out


@njit(cache=True)
def _roll_rescan_kernel(arr, window, min_count, mode):
    """沿第 0 轴重扫窗口计算无法增量维护的统计量（秩、连乘）。"""
    out = np.empty(arr.shape, dtype=np.float64)
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            for t in range(arr.shape[0]):
                lo = t - window + 1
                if lo < 0:
                    lo = 0
                count = 0
                product = 1.0
                current = arr[t, n, s]
                n_less = 0
                n_equal = 0
                for i in range(lo, t + 1):
                    value = arr[i, n, s]
                    if not np.isfinite(value):
                        continue
                    count += 1
                    product *= 1.0 + value
                    if mode == _ROLL_RANK and np.isfinite(current):
                        if value < current:
                            n_less += 1
                        elif value == current:
                            n_equal += 1
                if count < min_count:
                    out[t, n, s] = np.nan
                elif mode == _ROLL_CUMPROD:
                    out[t, n, s] = product
                elif not np.isfinite(current):
                    out[t, n, s] = np.nan
                elif count == 1:
                    out[t, n, s] = 0.5
                else:
                    out[t, n, s] = (n_less + 0.5 * (n_equal - 1)) / (count - 1)
    return out


@njit(cache=True)
def _roll_quantile_kernel(arr, window, min_count, quantile):
    """沿第 0 轴用滑动有序缓冲计算滚动分位数（线性插值）。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # buffer 始终保存当前窗口内有效值的升序排列，全部序列复用同一份分配。
    buffer = np.empty(window, dtype=np.float64)
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            k = 0
            for t in range(arr.shape[0]):
                # 先移除 window 步之前的过期值，再插入新值，保持 k 不超过 window。
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        pos = np.searchsorted(buffer[:k], expired)
                        for j in range(pos, k - 1):
                            buffer[j] = buffer[j + 1]
                        k -= 1
                value = arr[t, n, s]
                if np.isfinite(value):
                    pos = np.searchsorted(buffer[:k], value)
                    for j in range(k, pos, -1):
                        buffer[j] = buffer[j - 1]
                    buffer[pos] = value
                    k += 1
                if k < min_count:
                    out[t, n, s] = np.nan
                    continue
                pos_f = quantile * (k - 1)
                lower = int(pos_f)
                frac = pos_f - lower
                if lower + 1 < k:
                    out[t, n, s] = buffer[lower] + frac * (buffer[lower + 1] - buffer[lower])
                else:
                    out[t, n, s] = buffer[lower]
    return out


@njit(cache=True)
def _ts_change_kernel(arr, periods, percentage):
    """沿第 0 轴计算差分或百分比变化，越界或非法结果输出 nan。"""
    out = np.empty(arr.shape, dtype=np.float64)
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            for t in range(arr.shape[0]):
                source_t = t - periods
                if source_t < 0:
                    out[t, n, s] = np.nan
                    continue
                current = arr[t, n, s]
                previous = arr[source_t, n, s]
                if percentage:
                    value = (
                        (current - previous) / previous
                        if previous != 0.0
                        else np.nan
                    )
                else:
                    value = current - previous
                out[t, n, s] = value if np.isfinite(value) else np.nan
    return out


@njit(cache=True)
def _roll_pair_kernel(left, right, window, min_count, mode):
    """沿第 0 轴滑动维护成对有效样本的矩，输出协方差、相关系数或回归斜率。"""
    out = np.empty(left.shape, dtype=np.float64)
    # 逐条序列滑动：加入新成对值、移除 window 步之前的过期成对值。
    for n in range(left.shape[1]):
        for s in range(left.shape[2]):
            count = 0
            sum_x = 0.0
            sum_y = 0.0
            sum_xx = 0.0
            sum_yy = 0.0
            sum_xy = 0.0
            for t in range(left.shape[0]):
                xv = left[t, n, s]
                yv = right[t, n, s]
                if np.isfinite(xv) and np.isfinite(yv):
                    count += 1
                    sum_x += xv
                    sum_y += yv
                    sum_xx += xv * xv
                    sum_yy += yv * yv
                    sum_xy += xv * yv
                if t >= window:
                    old_x = left[t - window, n, s]
                    old_y = right[t - window, n, s]
                    if np.isfinite(old_x) and np.isfinite(old_y):
                        count -= 1
                        sum_x -= old_x
                        sum_y -= old_y
                        sum_xx -= old_x * old_x
                        sum_yy -= old_y * old_y
                        sum_xy -= old_x * old_y
                out[t, n, s] = _pair_stat(
                    count, sum_x, sum_y, sum_xx, sum_yy, sum_xy, min_count, mode
                )
    return out


@njit(cache=True, inline="always")
def _pair_stat(count, sum_x, sum_y, sum_xx, sum_yy, sum_xy, min_count, mode):
    """由成对累计矩输出协方差（ddof=1）、相关系数或回归斜率。"""
    if count < min_count or count < 2:
        return np.nan
    mean_y = sum_y / count
    cov_xy = sum_xy - sum_x * mean_y
    var_x = _population_variance(sum_x, sum_xx, count) * count
    var_y = _population_variance(sum_y, sum_yy, count) * count
    if mode == _PAIR_COV:
        return cov_xy / (count - 1)
    if mode == _PAIR_BETA:
        return cov_xy / var_x if var_x > 0.0 else np.nan
    if var_x > 0.0 and var_y > 0.0:
        return cov_xy / np.sqrt(var_x * var_y)
    return np.nan


@njit(cache=True)
def _roll_tbeta_kernel(arr, window, min_count):
    """沿第 0 轴滑动计算值对窗口内时间序号（1 起）的滚动回归斜率。

    回归斜率对时间轴的常数平移不变，因此滑动实现直接对绝对位置累计矩。
    """
    out = np.empty(arr.shape, dtype=np.float64)
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            count = 0
            sum_x = 0.0
            sum_y = 0.0
            sum_yy = 0.0
            sum_xy = 0.0
            for t in range(arr.shape[0]):
                value = arr[t, n, s]
                if np.isfinite(value):
                    time_index = float(t + 1)
                    count += 1
                    sum_x += value
                    sum_y += time_index
                    sum_yy += time_index * time_index
                    sum_xy += value * time_index
                if t >= window:
                    expired = arr[t - window, n, s]
                    if np.isfinite(expired):
                        old_index = float(t - window + 1)
                        count -= 1
                        sum_x -= expired
                        sum_y -= old_index
                        sum_yy -= old_index * old_index
                        sum_xy -= expired * old_index
                if count < min_count or count < 2:
                    out[t, n, s] = np.nan
                    continue
                mean_y = sum_y / count
                cov_xy = sum_xy - sum_x * mean_y
                var_y = sum_yy - sum_y * mean_y
                out[t, n, s] = cov_xy / var_y if var_y > 0.0 else np.nan
    return out


@njit(cache=True)
def _roll_rankcorr_kernel(left, right, window, min_count):
    """沿第 0 轴计算滚动 Spearman 秩相关系数（平局取中位秩）。"""
    out = np.empty(left.shape, dtype=np.float64)
    buffer_x = np.empty(window, dtype=np.float64)
    buffer_y = np.empty(window, dtype=np.float64)
    rank_x = np.empty(window, dtype=np.float64)
    rank_y = np.empty(window, dtype=np.float64)
    for n in range(left.shape[1]):
        for s in range(left.shape[2]):
            for t in range(left.shape[0]):
                lo = t - window + 1
                if lo < 0:
                    lo = 0
                k = 0
                for i in range(lo, t + 1):
                    xv = left[i, n, s]
                    yv = right[i, n, s]
                    if np.isfinite(xv) and np.isfinite(yv):
                        buffer_x[k] = xv
                        buffer_y[k] = yv
                        k += 1
                if k < min_count or k < 2:
                    out[t, n, s] = np.nan
                    continue
                _midrank(buffer_x, k, rank_x)
                _midrank(buffer_y, k, rank_y)
                out[t, n, s] = _midrank_corr(rank_x, rank_y, k)
    return out


@njit(cache=True)
def _midrank(values, count, out):
    """对前 count 个值计算中位秩（平局取平均秩），结果写入 out。"""
    order = np.argsort(values[:count])
    sorted_vals = values[:count][order]
    i = 0
    while i < count:
        j = i
        while j + 1 < count and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        midrank = (i + j) / 2.0 + 1.0
        for m in range(i, j + 1):
            out[order[m]] = midrank
        i = j + 1


@njit(cache=True, inline="always")
def _midrank_corr(rank_x, rank_y, count):
    """对两组中位秩计算 Pearson 相关系数。"""
    mean_rank = (count + 1) / 2.0
    sxx = 0.0
    syy = 0.0
    sxy = 0.0
    for i in range(count):
        dx = rank_x[i] - mean_rank
        dy = rank_y[i] - mean_rank
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx > 0.0 and syy > 0.0:
        return sxy / np.sqrt(sxx * syy)
    return np.nan


@njit(cache=True)
def _roll_split_kernel(left, right, window, top, min_count, mode):
    """沿第 0 轴对窗口内 right 最大的前 top 个样本统计 left。"""
    out = np.empty(left.shape, dtype=np.float64)
    buffer_x = np.empty(window, dtype=np.float64)
    buffer_y = np.empty(window, dtype=np.float64)
    for n in range(left.shape[1]):
        for s in range(left.shape[2]):
            for t in range(left.shape[0]):
                lo = t - window + 1
                if lo < 0:
                    lo = 0
                k = 0
                for i in range(lo, t + 1):
                    xv = left[i, n, s]
                    yv = right[i, n, s]
                    if np.isfinite(xv) and np.isfinite(yv):
                        buffer_x[k] = xv
                        buffer_y[k] = yv
                        k += 1
                if k < min_count:
                    out[t, n, s] = np.nan
                    continue
                take = top if top < k else k
                threshold = np.sort(buffer_y[:k])[k - take]
                out[t, n, s] = _split_stat(buffer_x, buffer_y, k, threshold, mode)
    return out


@njit(cache=True, inline="always")
def _split_stat(buffer_x, buffer_y, count, threshold, mode):
    """对 right >= threshold 的样本计算 left 的均值、总体标准差或与 right 的相关系数。"""
    selected = 0
    sum_x = 0.0
    sum_xx = 0.0
    sum_y = 0.0
    sum_yy = 0.0
    sum_xy = 0.0
    for i in range(count):
        if buffer_y[i] >= threshold:
            xv = buffer_x[i]
            yv = buffer_y[i]
            selected += 1
            sum_x += xv
            sum_xx += xv * xv
            sum_y += yv
            sum_yy += yv * yv
            sum_xy += xv * yv
    if selected == 0:
        return np.nan
    if mode == _SPLIT_MEAN:
        return sum_x / selected
    if mode == _SPLIT_STD:
        variance = _population_variance(sum_x, sum_xx, selected)
        return np.sqrt(variance) if variance >= 0.0 else np.nan
    if selected < 2:
        return np.nan
    mean_y = sum_y / selected
    cov_xy = sum_xy - sum_x * mean_y
    var_x = _population_variance(sum_x, sum_xx, selected) * selected
    var_y = _population_variance(sum_y, sum_yy, selected) * selected
    if var_x > 0.0 and var_y > 0.0:
        return cov_xy / np.sqrt(var_x * var_y)
    return np.nan


@njit(cache=True)
def _roll_ewm_kernel(arr, halflife, window, min_count, mode):
    """沿第 0 轴按半衰期指数衰减权重计算滚动加权均值或总体标准差。"""
    out = np.empty(arr.shape, dtype=np.float64)
    decay = np.log(2.0) / halflife
    for n in range(arr.shape[1]):
        for s in range(arr.shape[2]):
            for t in range(arr.shape[0]):
                lo = t - window + 1
                if lo < 0:
                    lo = 0
                count = 0
                weight_sum = 0.0
                total = 0.0
                total_sq = 0.0
                for i in range(lo, t + 1):
                    value = arr[i, n, s]
                    if not np.isfinite(value):
                        continue
                    weight = np.exp(-decay * (t - i))
                    count += 1
                    weight_sum += weight
                    total += weight * value
                    total_sq += weight * value * value
                if count < min_count or weight_sum <= 0.0:
                    out[t, n, s] = np.nan
                    continue
                mean = total / weight_sum
                if mode == _EWM_MEAN:
                    out[t, n, s] = mean
                else:
                    variance = _population_variance(total, total_sq, weight_sum)
                    out[t, n, s] = np.sqrt(variance) if variance >= 0.0 else np.nan
    return out


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
