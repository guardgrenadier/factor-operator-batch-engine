"""覆盖移植的截面算子的数值语义、样本掩码、布局与编译期校验。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    CompileError,
    ComputeRequest,
    DomainError,
    DomainSpec,
    InputSpec,
    MemoryDataProvider,
    ValueKind,
)
from factor_engine.formula import FormulaBatch


DATES = ["20240102", "20240103"]
CODES = [1, 2, 3, 4, 5]

# 2D (T, N) 供 provider 使用；3D (T, N, 1) 供参考计算使用。
X2 = np.array(
    [
        [1.0, 2.0, np.nan, 4.0, 5.0],
        [3.0, 1.0, 5.0, 2.0, 4.0],
    ]
)
Y2 = np.array(
    [
        [2.0, 1.0, 3.0, 5.0, 4.0],
        [1.0, 2.0, 4.0, 3.0, 5.0],
    ]
)
MASK2 = np.array([[1.0, 1.0, 1.0, 0.0, 1.0], [1.0, 1.0, 1.0, 1.0, 0.0]])
X = X2[:, :, None]
Y = Y2[:, :, None]
MASK = MASK2[:, :, None]


def _provider() -> MemoryDataProvider:
    """构造含双数值输入与样本掩码的内存数据提供方。"""
    return MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={"stk.1d.x": X2, "stk.1d.y": Y2, "stk.1d.sample": MASK2},
        input_specs={
            "stk.1d.sample": InputSpec("stk", "1d", 1, ValueKind.MASK),
        },
    )


def _compute(expression, formula_id="alpha"):
    """以给定表达式为因子输出执行完整计算，返回 T x N x 1 数组。"""
    provider = _provider()
    request = ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "x = source('stk.1d.x')\n"
                "y = source('stk.1d.y')\n"
                "sample = source('stk.1d.sample')"
            ),
            formulas={formula_id: f"factor = {expression}"},
        ),
    )
    return BatchFactorEngine(provider).compute(request).arrays[formula_id]


def _masked(values):
    """按样本掩码过滤截面，供参考计算使用。"""
    return np.where(MASK == 1.0, values, np.nan)


@pytest.mark.parametrize(
    ("expression", "reference"),
    [
        ("cs_median(x)", np.nanmedian),
        ("cs_var(x)", np.nanvar),
        ("cs_max(x)", np.nanmax),
        ("cs_min(x)", np.nanmin),
    ],
)
def test_cs_reducers_match_numpy(expression, reference) -> None:
    """验证截面归约算子与 NumPy 参考实现一致。"""
    result = _compute(expression)
    expected = np.broadcast_to(reference(X, axis=1, keepdims=True), X.shape)
    np.testing.assert_allclose(result, expected, equal_nan=True)


def test_cs_quantile_matches_numpy() -> None:
    """验证截面分位数与 NumPy 线性插值一致。"""
    result = _compute("cs_quantile(x, q=0.25)")
    expected = np.broadcast_to(
        np.nanquantile(X, 0.25, axis=1, keepdims=True), X.shape
    )
    np.testing.assert_allclose(result, expected, equal_nan=True)


def test_cs_count_matches_valid_count() -> None:
    """验证截面有效值数量。"""
    result = _compute("cs_count(x)")
    expected = np.broadcast_to(
        np.sum(np.isfinite(X), axis=1, keepdims=True), X.shape
    )
    np.testing.assert_allclose(result, expected)


def test_cs_cv_matches_numpy() -> None:
    """验证截面变异系数（总体标准差除以均值）。"""
    result = _compute("cs_cv(x)")
    expected = np.broadcast_to(
        np.nanstd(X, axis=1, keepdims=True) / np.nanmean(X, axis=1, keepdims=True),
        X.shape,
    )
    np.testing.assert_allclose(result, expected, equal_nan=True)


def test_cs_reducers_respect_sample_mask() -> None:
    """验证样本掩码过滤参与截面统计的样本。"""
    masked = _masked(X)
    np.testing.assert_allclose(
        _compute("cs_median(x, sample_mask=sample)"),
        np.broadcast_to(np.nanmedian(masked, axis=1, keepdims=True), X.shape),
        equal_nan=True,
    )
    np.testing.assert_allclose(
        _compute("cs_count(x, sample_mask=sample)")[:, 0, 0],
        np.sum(np.isfinite(masked[:, :, 0]), axis=1),
    )


def test_cs_skew_and_kurt_match_bias_corrected_formulas() -> None:
    """验证偏差修正的截面偏度与 Pearson 峰度。"""
    values = np.array([[3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]])
    provider = MemoryDataProvider(
        dates=["20240102"],
        asset_codes={"stk": list(range(8))},
        data={"stk.1d.x": values},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240102", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={
                "skew": "factor = cs_skew(x)",
                "kurt": "factor = cs_kurt(x)",
            },
        ),
    )
    result = BatchFactorEngine(provider).compute(request)
    flat = values[0]
    n = flat.size
    mean = flat.mean()
    m2 = ((flat - mean) ** 2).mean()
    m3 = ((flat - mean) ** 3).mean()
    m4 = ((flat - mean) ** 4).mean()
    skew = np.sqrt(n * (n - 1)) / (n - 2) * (m3 / m2**1.5)
    kurt = ((n * n - 1) * (m4 / m2**2) - 3 * (n - 1) ** 2) / ((n - 2) * (n - 3)) + 3
    np.testing.assert_allclose(result.arrays["skew"][0, :, 0], skew)
    np.testing.assert_allclose(result.arrays["kurt"][0, :, 0], kurt)


def test_cs_mad_matches_median_absolute_deviation() -> None:
    """验证截面中值绝对偏差。"""
    result = _compute("cs_mad(x)")
    median = np.nanmedian(X, axis=1, keepdims=True)
    expected = np.nanmedian(np.abs(X - median), axis=1, keepdims=True)
    np.testing.assert_allclose(
        result, np.broadcast_to(expected, X.shape), equal_nan=True
    )


def test_cs_entropy_and_rel_entropy_match_manual_computation() -> None:
    """验证香农熵与相对熵。"""
    p_base = np.abs(X)
    p = p_base / np.nansum(p_base, axis=1, keepdims=True)
    expected_entropy = -np.nansum(np.where(p > 0, p * np.log(p), 0.0), axis=1)
    np.testing.assert_allclose(
        _compute("cs_entropy(abs(x))")[:, 0, 0], expected_entropy[:, 0]
    )
    q_base = np.abs(Y)
    pair_valid = np.isfinite(p_base) & np.isfinite(q_base)
    p_pair = np.where(pair_valid, p_base, 0.0)
    q_pair = np.where(pair_valid, q_base, 0.0)
    p = p_pair / np.sum(p_pair, axis=1, keepdims=True)
    q = q_pair / np.sum(q_pair, axis=1, keepdims=True)
    expected_rel = np.sum(np.where((p > 0) & (q > 0), p * np.log(p / q), 0.0), axis=1)
    np.testing.assert_allclose(
        _compute("cs_rel_entropy(abs(x), abs(y))")[:, 0, 0], expected_rel[:, 0]
    )


def test_cs_cumprod_matches_cumulative_product() -> None:
    """验证截面 (1 + x) 连乘。"""
    result = _compute("cs_cumprod(x)")
    expected = np.nanprod(1.0 + X, axis=1, keepdims=True)
    np.testing.assert_allclose(
        result, np.broadcast_to(expected, X.shape), equal_nan=True
    )


def test_cs_pair_statistics_match_numpy() -> None:
    """验证截面协方差、相关系数与回归斜率。"""
    cov = _compute("cs_cov(x, y)")
    corr = _compute("cs_corr(x, y)")
    beta = _compute("cs_beta(x, y)")
    for t in range(X.shape[0]):
        xv = X[t, :, 0]
        yv = Y[t, :, 0]
        valid = np.isfinite(xv) & np.isfinite(yv)
        xv, yv = xv[valid], yv[valid]
        expected_cov = np.cov(xv, yv)[0, 1]
        np.testing.assert_allclose(cov[t, 0, 0], expected_cov)
        np.testing.assert_allclose(corr[t, 0, 0], np.corrcoef(xv, yv)[0, 1])
        expected_beta = np.sum((xv - xv.mean()) * (yv - yv.mean())) / np.sum(
            (xv - xv.mean()) ** 2
        )
        np.testing.assert_allclose(beta[t, 0, 0], expected_beta)


def test_cs_spearman_matches_pearson_on_midranks() -> None:
    """验证 Spearman 秩相关系数等于中位秩上的 Pearson 相关。"""
    result = _compute("cs_corr(x, y, method='spearman')")
    for t in range(X.shape[0]):
        xv = X[t, :, 0]
        yv = Y[t, :, 0]
        valid = np.isfinite(xv) & np.isfinite(yv)
        xv, yv = xv[valid], yv[valid]
        expected = np.corrcoef(_midrank(xv), _midrank(yv))[0, 1]
        np.testing.assert_allclose(result[t, 0, 0], expected)


def _midrank(values):
    """计算一维数组的中位秩（平局取平均秩）。"""
    order = np.argsort(values, kind="mergesort")
    sorted_vals = values[order]
    ranks = np.empty(len(values))
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def test_cs_min_max_scale_transforms_whole_cross_section() -> None:
    """验证 min-max 缩放保留资产轴并按样本极值缩放整个截面。"""
    result = _compute("cs_min_max_scale(x)")
    minimum = np.nanmin(X, axis=1, keepdims=True)
    maximum = np.nanmax(X, axis=1, keepdims=True)
    np.testing.assert_allclose(result, (X - minimum) / (maximum - minimum), equal_nan=True)


def test_cs_gauss_rank_matches_erfinv_of_pct_rank() -> None:
    """验证高斯化排名与百分位排名的 erfinv 变换一致。"""
    result = _compute("cs_gauss_rank(x)")
    expected_rank = _compute("rank(x)")
    scaled = np.clip((expected_rank - 0.5) * 2.0, -0.999999, 0.999999)
    # 参考实现使用 Abramowitz-Stegun 逼近 erf，精度约 1e-7。
    np.testing.assert_allclose(
        result, _erfinv_reference(scaled), equal_nan=True, rtol=1e-6, atol=1e-6
    )


def _erfinv_reference(z):
    """用 Winitzki 初值加 Newton 迭代计算参考 erfinv。"""
    z = np.asarray(z, dtype=np.float64)
    out = np.full_like(z, np.nan)
    valid = np.isfinite(z)
    signed = np.sign(z[valid])
    a = 0.147
    log_term = np.log(1.0 - z[valid] ** 2)
    first = 2.0 / (np.pi * a) + log_term / 2.0
    estimate = signed * np.sqrt(np.sqrt(first**2 - log_term / a) - first)
    for _ in range(8):
        error = _erf(estimate) - z[valid]
        derivative = 2.0 / np.sqrt(np.pi) * np.exp(-(estimate**2))
        estimate -= error / derivative
    out[valid] = estimate
    return out


def _erf(values):
    """用 Abramowitz-Stegun 7 阶逼近计算参考 erf。"""
    sign = np.sign(values)
    v = np.abs(values)
    t = 1.0 / (1.0 + 0.5 * v)
    tau = t * np.exp(
        -v * v
        - 1.26551223
        + t
        * (
            1.00002368
            + t
            * (
                0.37409196
                + t
                * (
                    0.09678418
                    + t
                    * (
                        -0.18628806
                        + t
                        * (
                            0.27886807
                            + t
                            * (
                                -1.13520398
                                + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277))
                            )
                        )
                    )
                )
            )
        )
    )
    return sign * (1.0 - tau)


def test_location_fills_position_index() -> None:
    """验证位置序号沿资产轴填充并传播缺失。"""
    result = _compute("location(x)")
    expected = np.where(
        np.isnan(X), np.nan, np.arange(1, X.shape[1] + 1, dtype=np.float64)[None, :, None]
    )
    np.testing.assert_allclose(result, expected, equal_nan=True)


def test_location_rejects_date_axis_at_compile_time() -> None:
    """验证日期轴位置序号在编译期被拒绝。"""
    with pytest.raises((CompileError, DomainError), match="date axis"):
        _compute("location(x, axis=0)")


def test_umr_matches_demeaned_product() -> None:
    """验证 (x - 截面均值) * y。"""
    result = _compute("umr(x, y)")
    mean = np.nanmean(X, axis=1, keepdims=True)
    np.testing.assert_allclose(result, (X - mean) * Y, equal_nan=True)


def test_ols_matches_cross_sectional_residual() -> None:
    """验证截面 OLS 残差与手工回归一致。"""
    result = _compute("ols(x, y)")
    for t in range(X.shape[0]):
        xv = X[t, :, 0]
        yv = Y[t, :, 0]
        valid = np.isfinite(xv) & np.isfinite(yv)
        xs, ys = xv[valid], yv[valid]
        beta = np.sum((xs - xs.mean()) * (ys - ys.mean())) / np.sum(
            (xs - xs.mean()) ** 2
        )
        intercept = ys.mean() - beta * xs.mean()
        expected = yv - beta * xv - intercept
        np.testing.assert_allclose(result[t, :, 0], expected, equal_nan=True)


def test_rank_compositions_match_pct_rank_arithmetic() -> None:
    """验证排名加减乘除组合算子。"""
    rank_x = _compute("rank(x)")
    rank_y = _compute("rank(y)")
    np.testing.assert_allclose(_compute("rank_add(x, y)"), rank_x + rank_y, equal_nan=True)
    np.testing.assert_allclose(_compute("rank_sub(x, y)"), rank_x - rank_y, equal_nan=True)
    np.testing.assert_allclose(_compute("rank_mul(x, y)"), rank_x * rank_y, equal_nan=True)
    np.testing.assert_allclose(
        _compute("rank_div(x, y)"), rank_x / rank_y, equal_nan=True
    )


def test_cs_corr_rejects_unknown_method_at_compile_time() -> None:
    """验证未知相关方法在编译期报错。"""
    with pytest.raises(CompileError, match="cs_corr method"):
        _compute("cs_corr(x, y, method='kendall')")


def test_cs_quantile_rejects_out_of_range_probability() -> None:
    """验证越界分位数在编译期报错。"""
    with pytest.raises(CompileError, match="q must be in"):
        _compute("cs_quantile(x, q=1.5)")
