"""截面算子：沿资产轴的统计、rank、缩尾、中性化与分组成员统计。"""

from __future__ import annotations

import math

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


def cs_median(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面中位数。"""
    return _cs_quantile_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 0.5)


def cs_quantile(x, sample_mask=None, q=0.5):
    """按 date/step 沿资产轴计算截面分位数（线性插值）。"""
    return _cs_quantile_kernel(
        x, _mask_arg(sample_mask), sample_mask is not None, float(q)
    )


def cs_var(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面总体方差（ddof=0）。"""
    return _cs_moment_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 0)


def cs_max(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面最大值。"""
    return _cs_minmax_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 0)


def cs_min(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面最小值。"""
    return _cs_minmax_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 1)


def cs_skew(x, sample_mask=None):
    """按 date/step 沿资产轴计算偏差修正的截面偏度（scipy bias=False）。"""
    return _cs_moment_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 1)


def cs_kurt(x, sample_mask=None):
    """按 date/step 沿资产轴计算偏差修正的截面 Pearson 峰度（正态为 3）。"""
    return _cs_moment_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 2)


def cs_cv(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面变异系数（总体标准差除以均值）。"""
    return _cs_moment_kernel(x, _mask_arg(sample_mask), sample_mask is not None, 3)


def cs_mad(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面中值绝对偏差。"""
    return _cs_mad_kernel(x, _mask_arg(sample_mask), sample_mask is not None)


def cs_entropy(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面香农熵，负值样本输出 nan。"""
    return _cs_entropy_kernel(x, _mask_arg(sample_mask), sample_mask is not None)


def cs_count(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面有效值数量。"""
    return _cs_count_kernel(x, _mask_arg(sample_mask), sample_mask is not None)


def cs_cumprod(x, sample_mask=None):
    """按 date/step 沿资产轴计算截面 (1 + x) 连乘积。"""
    return _cs_cumprod_kernel(x, _mask_arg(sample_mask), sample_mask is not None)


def cs_cov(x, y, sample_mask=None):
    """按 date/step 沿资产轴计算两输入的截面样本协方差（ddof=1）。"""
    return _cs_pair(x, y, sample_mask, 0)


def cs_corr(x, y, sample_mask=None, method="pearson"):
    """按 date/step 沿资产轴计算截面相关系数，支持 pearson 与 spearman。"""
    left, right, mask, has_mask = _cs_pair_inputs(x, y, sample_mask)
    if method == "spearman":
        return _cs_spearman_kernel(left, right, mask, has_mask)
    return _cs_pair_kernel(left, right, mask, has_mask, 1)


def cs_beta(x, y, sample_mask=None):
    """按 date/step 沿资产轴计算 y 对 x 的截面回归斜率。"""
    return _cs_pair(x, y, sample_mask, 2)


def cs_rel_entropy(x, y, sample_mask=None):
    """按 date/step 沿资产轴计算两输入归一化分布的相对熵（KL 散度）。"""
    left, right, mask, has_mask = _cs_pair_inputs(x, y, sample_mask)
    return _cs_rel_entropy_kernel(left, right, mask, has_mask)


def cs_min_max_scale(x, sample_mask=None):
    """按 date/step 用样本内极值把整个截面缩放到 [0, 1]。"""
    return _cs_min_max_scale_kernel(
        x, _mask_arg(sample_mask), sample_mask is not None
    )


def cs_gauss_rank(x, sample_mask=None):
    """按 date/step 对截面百分位排名做 erfinv 高斯化变换。"""
    return _gauss_rank_kernel(rank(x, sample_mask))


def location(x, axis=1):
    """沿资产或 step 轴填充从 1 开始的序号，输入缺失位置保持缺失。"""
    length = x.shape[axis]
    shape = [1, 1, 1]
    shape[axis] = length
    sequence = np.arange(1, length + 1, dtype=np.float64).reshape(shape)
    return np.where(np.isnan(x), np.nan, np.broadcast_to(sequence, x.shape))


def umr(x, y):
    """按 date/step 计算 (x - 截面均值) * y，输入广播对齐。"""
    left, right = np.broadcast_arrays(
        np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    )
    return _umr_kernel(left, right)


def ols(x, y, sample_mask=None):
    """按 date/step 沿资产轴回归 y ~ x 并返回残差（等价于中性化）。"""
    left, right = np.broadcast_arrays(
        np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    )
    return neutralize(right, left, sample_mask)


def rank_add(x, y):
    """按 date/step 计算两输入截面百分位排名之和。"""
    return rank(x) + rank(y)


def rank_div(x, y):
    """按 date/step 计算两输入截面百分位排名之商（保护除法）。"""
    return _rank_divide(rank(x), rank(y))


def rank_sub(x, y):
    """按 date/step 计算两输入截面百分位排名之差。"""
    return rank(x) - rank(y)


def rank_mul(x, y):
    """按 date/step 计算两输入截面百分位排名之积。"""
    return rank(x) * rank(y)


def _mask_arg(sample_mask):
    """把缺省样本掩码规范化为 kernel 使用的空数组哨兵。"""
    return _EMPTY_FLOAT_3D if sample_mask is None else sample_mask


def _cs_pair(x, y, sample_mask, mode):
    """双输入截面统计的公共入口。"""
    left, right, mask, has_mask = _cs_pair_inputs(x, y, sample_mask)
    return _cs_pair_kernel(left, right, mask, has_mask, mode)


def _cs_pair_inputs(x, y, sample_mask):
    """广播对齐双输入并规范化样本掩码。"""
    left, right = np.broadcast_arrays(
        np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    )
    return left, right, _mask_arg(sample_mask), sample_mask is not None


def _rank_divide(x, y):
    """排名相除的保护除法，分母为零或非有限结果转为 nan。"""
    left, right = np.broadcast_arrays(
        np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    )
    out = np.empty(np.broadcast_shapes(left.shape, right.shape), dtype=np.float64)
    np.divide(left, right, out=out, where=right != 0.0)
    out[right == 0.0] = np.nan
    out[np.isinf(out)] = np.nan
    return out


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


@njit(cache=True)
def _cs_quantile_kernel(arr, sample_mask, has_sample_mask, q):
    """按 date/step 收集截面有效样本并输出排序后的线性插值分位数。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    buffer = np.empty(arr.shape[1], dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    buffer[count] = value
                    count += 1
            if count == 0:
                out[t, 0, s] = np.nan
                continue
            sorted_vals = np.sort(buffer[:count])
            pos = q * (count - 1)
            lower = int(pos)
            frac = pos - lower
            if lower + 1 < count:
                out[t, 0, s] = sorted_vals[lower] + frac * (
                    sorted_vals[lower + 1] - sorted_vals[lower]
                )
            else:
                out[t, 0, s] = sorted_vals[lower]
    return out


@njit(cache=True)
def _cs_moment_kernel(arr, sample_mask, has_sample_mask, mode):
    """按 date/step 累计中心矩并输出方差、偏度、峰度或变异系数。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            total = 0.0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    total += value
            if count == 0:
                out[t, 0, s] = np.nan
                continue
            mean = total / count
            m2 = 0.0
            m3 = 0.0
            m4 = 0.0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    delta = value - mean
                    delta_sq = delta * delta
                    m2 += delta_sq
                    m3 += delta_sq * delta
                    m4 += delta_sq * delta_sq
            m2 /= count
            m3 /= count
            m4 /= count
            out[t, 0, s] = _moment_stat(count, mean, m2, m3, m4, mode)
    return out


@njit(cache=True, inline="always")
def _moment_stat(count, mean, m2, m3, m4, mode):
    """由总体中心矩输出方差、偏差修正偏度、Pearson 峰度或变异系数。"""
    if mode == 0:
        return m2
    if mode == 1:
        if count < 3 or m2 <= 0.0:
            return np.nan
        return np.sqrt(count * (count - 1)) / (count - 2) * (m3 / (m2 ** 1.5))
    if mode == 2:
        if count < 4 or m2 <= 0.0:
            return np.nan
        kurt = (count * count - 1.0) * (m4 / (m2 * m2)) - 3.0 * (count - 1) ** 2
        return kurt / ((count - 2) * (count - 3)) + 3.0
    if mean == 0.0 or m2 < 0.0:
        return np.nan
    return np.sqrt(m2) / mean


@njit(cache=True)
def _cs_minmax_kernel(arr, sample_mask, has_sample_mask, mode):
    """按 date/step 输出截面最值，mode 0 为最大、1 为最小。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            best = np.nan
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    if count == 0 or (value > best if mode == 0 else value < best):
                        best = value
                    count += 1
            out[t, 0, s] = best if count else np.nan
    return out


@njit(cache=True)
def _cs_mad_kernel(arr, sample_mask, has_sample_mask):
    """按 date/step 计算截面中值绝对偏差（对中位数的绝对偏差再取中位数）。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    values = np.empty(arr.shape[1], dtype=np.float64)
    deviations = np.empty(arr.shape[1], dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    values[count] = value
                    count += 1
            if count == 0:
                out[t, 0, s] = np.nan
                continue
            median = _sorted_median(values, count)
            for i in range(count):
                deviations[i] = abs(values[i] - median)
            out[t, 0, s] = _sorted_median(deviations, count)
    return out


@njit(cache=True, inline="always")
def _sorted_median(buffer, count):
    """对缓冲区内前 count 个值排序并返回中位数。"""
    sorted_vals = np.sort(buffer[:count])
    if count % 2 == 1:
        return sorted_vals[count // 2]
    return (sorted_vals[count // 2 - 1] + sorted_vals[count // 2]) / 2.0


@njit(cache=True)
def _cs_entropy_kernel(arr, sample_mask, has_sample_mask):
    """按 date/step 计算归一化截面的香农熵，负值或零和输出 nan。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            total = 0.0
            negative = False
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    total += value
                    negative = negative or value < 0.0
            if count == 0 or total <= 0.0 or negative:
                out[t, 0, s] = np.nan
                continue
            entropy = 0.0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value) and value > 0.0:
                    p = value / total
                    entropy -= p * np.log(p)
            out[t, 0, s] = entropy
    return out


@njit(cache=True)
def _cs_count_kernel(arr, sample_mask, has_sample_mask):
    """按 date/step 统计截面有效值数量。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                if np.isfinite(arr[t, n, s]):
                    count += 1
            out[t, 0, s] = float(count)
    return out


@njit(cache=True)
def _cs_cumprod_kernel(arr, sample_mask, has_sample_mask):
    """按 date/step 计算截面 (1 + x) 的连乘积。"""
    out = np.empty((arr.shape[0], 1, arr.shape[2]), dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            product = 1.0
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    count += 1
                    product *= 1.0 + value
            out[t, 0, s] = product if count else np.nan
    return out


@njit(cache=True)
def _cs_pair_kernel(left, right, sample_mask, has_sample_mask, mode):
    """按 date/step 累计成对截面矩并输出协方差、相关系数或回归斜率。"""
    out = np.empty((left.shape[0], 1, left.shape[2]), dtype=np.float64)
    for t in range(left.shape[0]):
        for s in range(left.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            sum_x = 0.0
            sum_y = 0.0
            sum_xx = 0.0
            sum_yy = 0.0
            sum_xy = 0.0
            for n in range(left.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                xv = left[t, n, s]
                yv = right[t, n, s]
                if np.isfinite(xv) and np.isfinite(yv):
                    count += 1
                    sum_x += xv
                    sum_y += yv
                    sum_xx += xv * xv
                    sum_yy += yv * yv
                    sum_xy += xv * yv
            out[t, 0, s] = _cs_pair_stat(
                count, sum_x, sum_y, sum_xx, sum_yy, sum_xy, mode
            )
    return out


@njit(cache=True, inline="always")
def _cs_pair_stat(count, sum_x, sum_y, sum_xx, sum_yy, sum_xy, mode):
    """由截面成对累计矩输出样本协方差（ddof=1）、相关系数或回归斜率。"""
    if count < 2:
        return np.nan
    mean_y = sum_y / count
    cov_xy = sum_xy - sum_x * mean_y
    var_x = _population_variance(sum_x, sum_xx, count) * count
    var_y = _population_variance(sum_y, sum_yy, count) * count
    if mode == 0:
        return cov_xy / (count - 1)
    if mode == 2:
        return cov_xy / var_x if var_x > 0.0 else np.nan
    if var_x > 0.0 and var_y > 0.0:
        return cov_xy / np.sqrt(var_x * var_y)
    return np.nan


@njit(cache=True)
def _cs_spearman_kernel(left, right, sample_mask, has_sample_mask):
    """按 date/step 对截面中位秩计算 Spearman 秩相关系数。"""
    out = np.empty((left.shape[0], 1, left.shape[2]), dtype=np.float64)
    buffer_x = np.empty(left.shape[1], dtype=np.float64)
    buffer_y = np.empty(left.shape[1], dtype=np.float64)
    rank_x = np.empty(left.shape[1], dtype=np.float64)
    rank_y = np.empty(left.shape[1], dtype=np.float64)
    for t in range(left.shape[0]):
        for s in range(left.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            for n in range(left.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                xv = left[t, n, s]
                yv = right[t, n, s]
                if np.isfinite(xv) and np.isfinite(yv):
                    buffer_x[count] = xv
                    buffer_y[count] = yv
                    count += 1
            if count < 2:
                out[t, 0, s] = np.nan
                continue
            _cs_midrank(buffer_x, count, rank_x)
            _cs_midrank(buffer_y, count, rank_y)
            out[t, 0, s] = _cs_midrank_corr(rank_x, rank_y, count)
    return out


@njit(cache=True)
def _cs_midrank(values, count, out):
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
def _cs_midrank_corr(rank_x, rank_y, count):
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
def _cs_rel_entropy_kernel(left, right, sample_mask, has_sample_mask):
    """按 date/step 计算两个归一化截面的相对熵（KL 散度）。"""
    out = np.empty((left.shape[0], 1, left.shape[2]), dtype=np.float64)
    for t in range(left.shape[0]):
        for s in range(left.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            total_x = 0.0
            total_y = 0.0
            negative = False
            for n in range(left.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                xv = left[t, n, s]
                yv = right[t, n, s]
                if np.isfinite(xv) and np.isfinite(yv):
                    count += 1
                    total_x += xv
                    total_y += yv
                    negative = negative or xv < 0.0 or yv < 0.0
            if count == 0 or total_x <= 0.0 or total_y <= 0.0 or negative:
                out[t, 0, s] = np.nan
                continue
            divergence = 0.0
            valid = True
            for n in range(left.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                xv = left[t, n, s]
                yv = right[t, n, s]
                if not (np.isfinite(xv) and np.isfinite(yv)):
                    continue
                if xv == 0.0:
                    continue
                if yv == 0.0:
                    valid = False
                    break
                p = xv / total_x
                q = yv / total_y
                divergence += p * np.log(p / q)
            out[t, 0, s] = divergence if valid else np.nan
    return out


@njit(cache=True)
def _cs_min_max_scale_kernel(arr, sample_mask, has_sample_mask):
    """按 date/step 用样本内极值把整个截面缩放到 [0, 1]，极值相等输出 nan。"""
    out = np.empty(arr.shape, dtype=np.float64)
    for t in range(arr.shape[0]):
        for s in range(arr.shape[2]):
            mask_step = 0 if has_sample_mask and sample_mask.shape[2] == 1 else s
            count = 0
            minimum = np.nan
            maximum = np.nan
            for n in range(arr.shape[1]):
                if has_sample_mask and sample_mask[t, n, mask_step] != 1.0:
                    continue
                value = arr[t, n, s]
                if np.isfinite(value):
                    if count == 0 or value < minimum:
                        minimum = value
                    if count == 0 or value > maximum:
                        maximum = value
                    count += 1
            span = maximum - minimum
            for n in range(arr.shape[1]):
                value = arr[t, n, s]
                if not np.isfinite(value) or count == 0 or span <= 0.0:
                    out[t, n, s] = np.nan
                else:
                    out[t, n, s] = (value - minimum) / span
    return out


@njit(cache=True)
def _gauss_rank_kernel(ranked):
    """把 (0, 1] 的百分位排名映射到标准正态分位数。"""
    out = np.empty(ranked.shape, dtype=np.float64)
    for index in np.ndindex(ranked.shape):
        value = ranked[index]
        if np.isnan(value):
            out[index] = np.nan
            continue
        scaled = (value - 0.5) * 2.0
        if scaled >= 1.0:
            scaled = 0.999999
        elif scaled <= -1.0:
            scaled = -0.999999
        out[index] = _erfinv_scalar(scaled)
    return out


@njit(cache=True, inline="always")
def _erfinv_scalar(y):
    """用 Winitzki 初值加 Newton 迭代计算误差函数的反函数。"""
    if y <= -1.0:
        return -np.inf if y == -1.0 else np.nan
    if y >= 1.0:
        return np.inf if y == 1.0 else np.nan
    coeff = 0.147
    log_term = np.log(1.0 - y * y)
    first = 2.0 / (np.pi * coeff) + log_term / 2.0
    estimate = np.sign(y) * np.sqrt(np.sqrt(first * first - log_term / coeff) - first)
    # Newton 迭代在 |y| < 1 上快速收敛到机器精度。
    for _ in range(5):
        error = math.erf(estimate) - y
        derivative = 2.0 / np.sqrt(np.pi) * np.exp(-estimate * estimate)
        estimate -= error / derivative
    return estimate


@njit(cache=True)
def _umr_kernel(left, right):
    """按 date/step 计算 (left - 截面均值) * right，缺失向结果传播。"""
    out = np.empty(left.shape, dtype=np.float64)
    for t in range(left.shape[0]):
        for s in range(left.shape[2]):
            count = 0
            total = 0.0
            for n in range(left.shape[1]):
                value = left[t, n, s]
                if np.isfinite(value):
                    count += 1
                    total += value
            if count == 0:
                for n in range(left.shape[1]):
                    out[t, n, s] = np.nan
                continue
            mean = total / count
            for n in range(left.shape[1]):
                xv = left[t, n, s]
                yv = right[t, n, s]
                if np.isfinite(xv) and np.isfinite(yv):
                    out[t, n, s] = (xv - mean) * yv
                else:
                    out[t, n, s] = np.nan
    return out
