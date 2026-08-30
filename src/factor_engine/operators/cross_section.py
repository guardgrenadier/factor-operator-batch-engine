"""截面算子：沿资产轴的统计、rank、缩尾、中性化与分组成员统计。"""

from __future__ import annotations

import numpy as np
from numba import njit


_EMPTY_FLOAT_3D = np.empty((0, 0, 0), dtype=np.float64)


def cs_mean(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面均值。"""
    return _cs_reduce_kernel(
        x,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
        0,
    )


def cs_sum(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面和。"""
    return _cs_reduce_kernel(
        x,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
        1,
    )


def cs_std(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面标准差。"""
    return _cs_reduce_kernel(
        x,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
        2,
    )


def cs_zscore(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面 zscore。"""
    return _cs_zscore_kernel(
        x,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
    )


def rank(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面 rank。"""
    # 每个日期和 step 独立筛选有效样本并计算稳定顺序排名。
    arr = x
    mask = sample_mask
    out = np.full_like(arr, np.nan, dtype=float)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            valid = np.isfinite(arr[t, :, s])
            if mask is not None:
                valid &= mask[t, :, 0 if mask.shape[2] == 1 else s] == 1.0
            idx = np.where(valid)[0]
            if len(idx) == 0:
                continue
            order = np.argsort(arr[t, idx, s], kind="mergesort")
            ranks = np.empty(len(idx), dtype=float)
            ranks[order] = np.arange(1, len(idx) + 1, dtype=float)
            out[t, idx, s] = ranks / len(idx)
    return out


def winsorize(x, sample_mask=None, lower=0.01, upper=0.99):
    """按 date/step 沿资产轴做分位数缩尾。"""
    # 分位点只由样本内有效资产估计，但裁剪作用于整个截面。
    arr = x
    mask = sample_mask
    out = arr.copy()
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            valid = np.isfinite(arr[t, :, s])
            if mask is not None:
                valid &= mask[t, :, 0 if mask.shape[2] == 1 else s] == 1.0
            vals = arr[t, valid, s]
            if len(vals) == 0:
                continue
            lo = np.nanquantile(vals, float(lower))
            hi = np.nanquantile(vals, float(upper))
            out[t, :, s] = np.clip(out[t, :, s], lo, hi)
    return out


def neutralize(x, exposure, sample_mask=None):
    """按 date/step 对单个 exposure 回归并返回残差。"""
    return _neutralize_kernel(
        x,
        exposure,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
    )


def group_mean(x, group, sample_mask=None, weight=None):
    """按分组在资产轴内计算均值。"""
    return _group_stat(x, group, sample_mask=sample_mask, weight=weight, mode=0)


def group_sum(x, group, sample_mask=None, weight=None):
    """按分组在资产轴内求和。"""
    return _group_stat(x, group, sample_mask=sample_mask, weight=weight, mode=1)


def group_std(x, group, sample_mask=None, weight=None):
    """按分组在资产轴内计算标准差。"""
    return _group_stat(x, group, sample_mask=sample_mask, weight=weight, mode=2)


def group_demean(x, group, sample_mask=None, weight=None):
    """按分组均值对输入去均值。"""
    return _group_stat(x, group, sample_mask=sample_mask, weight=weight, mode=3)


def group_zscore(x, group, sample_mask=None, weight=None):
    """按分组在资产轴内计算 zscore。"""
    return _group_stat(x, group, sample_mask=sample_mask, weight=weight, mode=4)


def member_mean(x, member, sample_mask=None, weight=None):
    """在指数成分内计算均值。"""
    return _member_stat(x, member, sample_mask=sample_mask, weight=weight, mode=0)


def member_sum(x, member, sample_mask=None, weight=None):
    """在指数成分内求和。"""
    return _member_stat(x, member, sample_mask=sample_mask, weight=weight, mode=1)


def member_std(x, member, sample_mask=None, weight=None):
    """在指数成分内计算标准差。"""
    return _member_stat(x, member, sample_mask=sample_mask, weight=weight, mode=2)


def member_demean(x, member, sample_mask=None, weight=None):
    """按指数成分内均值去均值。"""
    return _member_stat(x, member, sample_mask=sample_mask, weight=weight, mode=3)


def member_zscore(x, member, sample_mask=None, weight=None):
    """按指数成分内均值和标准差计算 zscore。"""
    return _member_stat(x, member, sample_mask=sample_mask, weight=weight, mode=4)


def _group_stat(x, group, *, sample_mask=None, weight=None, mode):
    """准备 group 统计输入并调用 Numba kernel。"""
    return _group_stat_kernel(
        x,
        group,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
        _EMPTY_FLOAT_3D if weight is None else weight,
        weight is not None,
        mode,
    )


def _member_stat(x, member, *, sample_mask=None, weight=None, mode):
    """准备 member 统计输入并调用 Numba kernel。"""
    # reduce 直接输出单资产轴，变换类统计则保留原资产轴。
    if mode <= 2:
        return _member_reduce_kernel(
            x,
            member,
            _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
            sample_mask is not None,
            _EMPTY_FLOAT_3D if weight is None else weight,
            weight is not None,
            mode,
        )
    return _member_transform_kernel(
        x,
        member,
        _EMPTY_FLOAT_3D if sample_mask is None else sample_mask,
        sample_mask is not None,
        _EMPTY_FLOAT_3D if weight is None else weight,
        weight is not None,
        mode,
    )


def _selector_3d(value, shape, *, name):
    """以无复制 view 对齐 singleton step。"""
    return np.broadcast_to(value, shape) if value.shape[2] == 1 else value


@njit(cache=True)
def _cs_reduce_kernel(arr, sample_mask, has_sample_mask, mode):
    """单次扫描资产轴并直接生成 T x 1 x S 结果。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    # 逐 date/step 累计有效值，再按 mode 输出均值、和或标准差。
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            count = 0
            total = 0.0
            total_sq = 0.0
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if not np.isfinite(value):
                    continue
                count += 1
                total += value
                total_sq += value * value
            if count == 0:
                out[t, 0, s] = np.nan
            elif mode == 0:
                out[t, 0, s] = total / count
            elif mode == 1:
                out[t, 0, s] = total
            else:
                variance = _population_variance(total, total_sq, count)
                out[t, 0, s] = np.sqrt(variance) if variance >= 0.0 else np.nan
    return out


@njit(cache=True)
def _cs_zscore_kernel(arr, sample_mask, has_sample_mask):
    """每个截面只累计一次矩，再直接写出 zscore。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 第一遍累计截面矩，第二遍逐资产标准化写出 zscore。
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            count = 0
            total = 0.0
            total_sq = 0.0
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    total += value
                    total_sq += value * value
            mean = total / count if count else np.nan
            variance = _population_variance(total, total_sq, count) if count else np.nan
            std = np.sqrt(variance) if variance > 0.0 else np.nan
            for n in range(arr.shape[1]):
                value = (arr[t, n, s] - mean) / std
                out[t, n, s] = value if np.isfinite(value) else np.nan
    return out


@njit(cache=True)
def _neutralize_kernel(arr, exposure, sample_mask, has_sample_mask):
    """用单暴露闭式 OLS 直接计算残差，不物化设计矩阵。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 先累计回归充分统计量，再逐资产写出残差。
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            count = 0
            sum_y = 0.0
            sum_e = 0.0
            sum_ee = 0.0
            sum_ey = 0.0
            exposure_step = 0 if exposure.shape[2] == 1 else s
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            for n in range(arr.shape[1]):
                y = arr[t, n, s]
                e = exposure[t, n, exposure_step]
                if not np.isfinite(y) or not np.isfinite(e):
                    continue
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                count += 1
                sum_y += y
                sum_e += e
                sum_ee += e * e
                sum_ey += e * y
            if count < 2:
                for n in range(arr.shape[1]):
                    out[t, n, s] = np.nan
                continue
            mean_y = sum_y / count
            mean_e = sum_e / count
            denominator = sum_ee - sum_e * mean_e
            beta = (sum_ey - sum_e * mean_y) / denominator if denominator > 0.0 else 0.0
            intercept = mean_y - beta * mean_e
            for n in range(arr.shape[1]):
                y = arr[t, n, s]
                e = exposure[t, n, exposure_step]
                value = y - intercept - beta * e
                out[t, n, s] = value if np.isfinite(value) else np.nan
    return out


@njit(cache=True)
def _group_stat_kernel(
    arr, group, sample_mask, has_sample_mask, weight, has_weight, mode
):
    """按日期、步长和分组计算带可选样本及权重的截面统计。"""
    out = np.empty(arr.shape, dtype=np.float64)
    # 预分配 2 的幂容量哈希表与分组矩缓冲区。
    t_count, n_count, s_count = arr.shape
    capacity = 1
    while capacity < n_count * 2:
        capacity *= 2
    hash_keys = np.empty(capacity, dtype=np.int64)
    hash_values = np.empty(capacity, dtype=np.int64)
    totals = np.empty(n_count, dtype=np.float64)
    totals_sq = np.empty(n_count, dtype=np.float64)
    denominators = np.empty(n_count, dtype=np.float64)
    for t in range(t_count):
        for s in range(s_count):
            hash_values[:] = -1
            group_count = 0
            group_step = 0 if group.shape[2] == 1 else s
            sample_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            weight_step = 0 if has_weight and weight.shape[2] == 1 else s
            # 第一遍扫描按有效分组建哈希表并累计加权矩。
            for n in range(n_count):
                group_value = group[t, n, group_step]
                if not np.isfinite(group_value) or group_value < 0.0:
                    continue
                g = int(group_value)
                if has_sample_mask and sample_mask[t, n, sample_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if not np.isfinite(value):
                    continue
                current_weight = 1.0
                if has_weight:
                    current_weight = weight[t, n, weight_step]
                    if not np.isfinite(current_weight) or current_weight <= 0.0:
                        continue
                slot = g & (capacity - 1)
                while hash_values[slot] >= 0 and hash_keys[slot] != g:
                    slot = (slot + 1) & (capacity - 1)
                group_pos = hash_values[slot]
                if group_pos < 0:
                    group_pos = group_count
                    group_count += 1
                    hash_keys[slot] = g
                    hash_values[slot] = group_pos
                    totals[group_pos] = 0.0
                    totals_sq[group_pos] = 0.0
                    denominators[group_pos] = 0.0
                totals[group_pos] += value * current_weight
                totals_sq[group_pos] += value * value * current_weight
                denominators[group_pos] += current_weight

            # 第二遍扫描按分组查询矩并写出统计值或变换结果。
            for n in range(n_count):
                group_value = group[t, n, group_step]
                if (
                    not np.isfinite(group_value)
                    or group_value < 0.0
                    or (has_sample_mask and sample_mask[t, n, sample_step] != 1.0)
                ):
                    out[t, n, s] = np.nan
                    continue
                g = int(group_value)
                slot = g & (capacity - 1)
                while hash_values[slot] >= 0 and hash_keys[slot] != g:
                    slot = (slot + 1) & (capacity - 1)
                group_pos = hash_values[slot]
                if group_pos < 0:
                    out[t, n, s] = np.nan
                    continue
                mean = totals[group_pos] / denominators[group_pos]
                variance = _population_variance(
                    totals[group_pos], totals_sq[group_pos], denominators[group_pos]
                )
                std = np.sqrt(variance) if variance >= 0.0 else np.nan
                stat = totals[group_pos] if mode == 1 else mean
                if mode == 2:
                    stat = std
                if mode <= 2:
                    out[t, n, s] = stat
                elif mode == 3:
                    out[t, n, s] = arr[t, n, s] - mean
                else:
                    value = (arr[t, n, s] - mean) / std
                    out[t, n, s] = value if np.isfinite(value) else np.nan
    return out


@njit(cache=True, inline="always")
def _member_moments(
    arr, member, sample_mask, has_sample_mask, weight, has_weight, t, s
):
    """单次扫描一个 date/step 的成员样本并返回统计矩。"""
    # 仅成员且通过样本掩码的有限值参与统计。
    count = 0
    sum_value = 0.0
    sum_sq = 0.0
    denominator = 0.0
    member_step = 0 if member.shape[2] == 1 else s
    sample_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
    weight_step = 0 if has_weight and weight.shape[2] == 1 else s
    for n in range(arr.shape[1]):
        if member[t, n, member_step] != 1.0:
            continue
        if has_sample_mask and sample_mask[t, n, sample_step] != 1.0:
            continue
        value = arr[t, n, s]
        if not np.isfinite(value):
            continue
        if has_weight:
            current_weight = weight[t, n, weight_step]
            if not np.isfinite(current_weight) or current_weight <= 0.0:
                continue
            sum_value += value * current_weight
            sum_sq += value * value * current_weight
            denominator += current_weight
        else:
            sum_value += value
            sum_sq += value * value
            denominator += 1.0
        count += 1
    if count == 0:
        return 0, np.nan, np.nan, np.nan
    # 由累计矩计算总体均值和标准差，并消除浮点舍入负方差。
    mean = sum_value / denominator
    variance = _population_variance(sum_value, sum_sq, denominator)
    std = np.sqrt(variance) if variance >= 0.0 else np.nan
    return count, sum_value, mean, std


@njit(cache=True, inline="always")
def _population_variance(total, total_sq, denominator):
    """由累计矩计算总体方差，并消除浮点舍入产生的微小负值。"""
    mean = total / denominator
    variance = total_sq / denominator - mean * mean
    scale = max(1.0, abs(total_sq / denominator), mean * mean)
    return 0.0 if abs(variance) <= 1e-12 * scale else variance


@njit(cache=True)
def _member_reduce_kernel(
    arr, member, sample_mask, has_sample_mask, weight, has_weight, mode
):
    """直接产生 T x 1 x S 的成员池 reduce，不物化资产轴临时结果。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            count, total, mean, std = _member_moments(
                arr,
                member,
                sample_mask,
                has_sample_mask,
                weight,
                has_weight,
                t,
                s,
            )
            if count == 0:
                out[t, 0, s] = np.nan
            elif mode == 0:
                out[t, 0, s] = mean
            elif mode == 1:
                out[t, 0, s] = total
            else:
                out[t, 0, s] = std
    return out


@njit(cache=True)
def _member_transform_kernel(
    arr, member, sample_mask, has_sample_mask, weight, has_weight, mode
):
    """产生 T x N x S 的成员池 demean/zscore。"""
    out = np.empty(arr.shape, dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            count, _, mean, std = _member_moments(
                arr,
                member,
                sample_mask,
                has_sample_mask,
                weight,
                has_weight,
                t,
                s,
            )
            member_step = 0 if member.shape[2] == 1 else s
            sample_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            for n in range(arr.shape[1]):
                selected = count > 0 and member[t, n, member_step] == 1.0
                if has_sample_mask and sample_mask[t, n, sample_step] != 1.0:
                    selected = False
                if not selected:
                    out[t, n, s] = np.nan
                elif mode == 3:
                    out[t, n, s] = arr[t, n, s] - mean
                else:
                    value = (arr[t, n, s] - mean) / std
                    out[t, n, s] = value if np.isfinite(value) else np.nan
    return out
