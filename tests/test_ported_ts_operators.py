"""覆盖移植的时序算子的数值语义、lookback 契约、分区一致性与编译期校验。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    CompileError,
    ComputeRequest,
    DomainSpec,
    ExecutionOptions,
    MemoryDataProvider,
)
from factor_engine.formula import FormulaBatch


DATES = ["20240102", "20240103", "20240104", "20240105", "20240108", "20240109"]

X2 = np.array(
    [
        [1.0, 4.0],
        [2.0, np.nan],
        [3.0, 6.0],
        [np.nan, 8.0],
        [5.0, 10.0],
        [6.0, 12.0],
    ]
)
Y2 = np.array(
    [
        [2.0, 1.0],
        [1.0, 3.0],
        [4.0, 2.0],
        [3.0, 5.0],
        [6.0, 4.0],
        [5.0, 7.0],
    ]
)


def _provider() -> MemoryDataProvider:
    """构造含双数值输入的六日内存数据提供方。"""
    return MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": [1, 2]},
        data={"stk.1d.x": X2, "stk.1d.y": Y2},
    )


def _request(expression, formula_id="alpha"):
    """构造以给定表达式为因子输出的计算请求。"""
    return ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')\ny = source('stk.1d.y')",
            formulas={formula_id: f"factor = {expression}"},
        ),
    )


def _compute(expression, formula_id="alpha", chunk_size=None):
    """执行计算并可选择分区大小，返回 T x N x 1 数组。"""
    engine = BatchFactorEngine(_provider())
    options = None if chunk_size is None else ExecutionOptions(chunk_size=chunk_size)
    return engine.compute(_request(expression, formula_id), options=options).arrays[
        formula_id
    ]


def _naive_window(arr, window, func, min_periods=None):
    """沿第 0 轴对 T x N x 1 数组做朴素滚动窗口计算。"""
    min_count = window if min_periods is None else min_periods
    out = np.full(arr.shape, np.nan)
    for t in range(arr.shape[0]):
        lo = max(0, t - window + 1)
        for n in range(arr.shape[1]):
            window_values = arr[lo : t + 1, n, 0]
            valid = window_values[np.isfinite(window_values)]
            if len(valid) >= min_count:
                out[t, n, 0] = func(valid)
    return out


X3 = X2[:, :, None]


@pytest.mark.parametrize(
    ("expression", "reference"),
    [
        ("ts_median(x, 3)", lambda v: np.median(v)),
        ("ts_var(x, 3)", lambda v: np.var(v)),
        ("ts_cumprod(x, 3)", lambda v: np.prod(1.0 + v)),
        ("ts_max_to_min(x, 3)", lambda v: np.max(v) - np.min(v)),
        ("ts_max(x, 3)", lambda v: np.max(v)),
        ("ts_min(x, 3)", lambda v: np.min(v)),
        ("ts_quantile(x, 3, quantile=0.25)", lambda v: np.quantile(v, 0.25)),
    ],
)
def test_rolling_unary_operators_match_naive(expression, reference) -> None:
    """验证单输入滚动算子与朴素窗口参考一致。"""
    result = _compute(expression)
    expected = _naive_window(X3, 3, reference)
    np.testing.assert_allclose(result, expected, equal_nan=True)


@pytest.mark.parametrize(
    ("expression", "lookback"),
    [
        ("ts_median(x, 3)", 2),
        ("ts_var(x, 4)", 3),
        ("ts_cumprod(x, 2)", 1),
        ("ts_max_to_min(x, 5)", 4),
        ("ts_quantile(x)", 9),
        ("ts_quantile(x, 7)", 6),
        ("ts_ewm_mean(x)", 9),
        ("ts_ewm_mean(x, 2, 4)", 3),
        ("ts_corr(x, y, 3)", 2),
        ("ts_split_mean(x, y, 4, 2)", 3),
        ("ts_diff(x, 2)", 2),
        ("ts_pct_change(x)", 1),
        ("ts_median(x, 3, axis=2)", 0),
        ("ts_corr(x, y, 3, axis=1)", 0),
    ],
)
def test_rolling_operator_lookback(expression, lookback) -> None:
    """验证滚动算子的日期回看与窗口参数精确对应。"""
    job = BatchFactorEngine(_provider()).compile(_request(expression))
    assert job.plan.job_lookback == lookback


@pytest.mark.parametrize(
    "expression",
    [
        "ts_median(x, 3)",
        "ts_quantile(x, 4, quantile=0.3)",
        "ts_corr(x, y, 3)",
        "ts_rankcorr(x, y, 3)",
        "ts_split_mean(x, y, 3, 1)",
        "ts_split_corr(x, y, 4, 2)",
        "ts_ewm_mean(x, 2, 4)",
        "ts_ewm_std(x, 2, 4)",
        "ts_tbeta(x, 3)",
        "ts_rank(x, 3)",
        "ts_argmin(x, 3)",
        "ts_pct_change(x, 2)",
    ],
)
def test_chunked_results_match_whole_domain(expression) -> None:
    """验证含回看的滚动算子在分区边界两侧结果一致。"""
    whole = _compute(expression)
    chunked = _compute(expression, chunk_size=2)
    np.testing.assert_allclose(whole, chunked, equal_nan=True)


def test_ts_argmin_argmax_report_periods_back() -> None:
    """验证最值位置输出为距今的回溯步数。"""
    values = np.array([[3.0, 1.0], [1.0, 2.0], [2.0, 6.0], [5.0, 4.0], [4.0, 8.0], [6.0, 3.0]])
    provider = MemoryDataProvider(
        dates=DATES, asset_codes={"stk": [1, 2]}, data={"stk.1d.x": values}
    )
    request = ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={"argmin": "factor = ts_argmin(x, 3)", "argmax": "factor = ts_argmax(x, 3)"},
        ),
    )
    result = BatchFactorEngine(provider).compute(request)
    np.testing.assert_allclose(
        result.arrays["argmin"][:, 0, 0],
        [np.nan, np.nan, 1.0, 2.0, 2.0, 1.0],
        equal_nan=True,
    )
    np.testing.assert_allclose(
        result.arrays["argmax"][:, 0, 0],
        [np.nan, np.nan, 2.0, 0.0, 1.0, 0.0],
        equal_nan=True,
    )


def test_ts_rank_reports_current_value_percentile() -> None:
    """验证当前值在窗口内的百分位排名（平局取中位秩）。"""
    values = np.array([[1.0], [3.0], [2.0], [2.0], [5.0], [4.0]])
    provider = MemoryDataProvider(
        dates=DATES, asset_codes={"stk": [1]}, data={"stk.1d.x": values}
    )
    request = ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={"alpha": "factor = ts_rank(x, 3)"},
        ),
    )
    result = BatchFactorEngine(provider).compute(request)
    # 窗口 [1,3,2] 当前 2: (1 + 0) / 2 = 0.5；[3,2,2] 当前 2: (0 + 0.5) / 2 = 0.25
    np.testing.assert_allclose(
        result.arrays["alpha"][:, 0, 0],
        [np.nan, np.nan, 0.5, 0.25, 1.0, 0.5],
        equal_nan=True,
    )


def test_ts_diff_and_pct_change_match_naive() -> None:
    """验证差分与百分比变化。"""
    diff = _compute("ts_diff(x, 2)")
    pct = _compute("ts_pct_change(x, 2)")
    lagged = np.full_like(X3, np.nan)
    lagged[2:] = X3[:-2]
    np.testing.assert_allclose(diff, X3 - lagged, equal_nan=True)
    expected_pct = np.where(
        np.isfinite(lagged) & (lagged != 0.0), (X3 - lagged) / lagged, np.nan
    )
    np.testing.assert_allclose(pct, expected_pct, equal_nan=True)


def test_ts_corr_cov_beta_match_naive() -> None:
    """验证滚动相关、协方差与回归斜率。"""
    corr = _compute("ts_corr(x, y, 3)")
    cov = _compute("ts_cov(x, y, 3)")
    beta = _compute("ts_beta(x, y, 3)")
    for t in range(X3.shape[0]):
        lo = max(0, t - 2)
        for n in range(2):
            xv = X3[lo : t + 1, n, 0]
            yv = Y2[lo : t + 1, n]
            valid = np.isfinite(xv) & np.isfinite(yv)
            if valid.sum() < 3:
                continue
            xs, ys = xv[valid], yv[valid]
            np.testing.assert_allclose(corr[t, n, 0], np.corrcoef(xs, ys)[0, 1])
            np.testing.assert_allclose(cov[t, n, 0], np.cov(xs, ys)[0, 1])
            expected_beta = np.sum((xs - xs.mean()) * (ys - ys.mean())) / np.sum(
                (xs - xs.mean()) ** 2
            )
            np.testing.assert_allclose(beta[t, n, 0], expected_beta)


def test_ts_tbeta_matches_regression_on_time_index() -> None:
    """验证对窗口内时间序号的滚动回归斜率。"""
    result = _compute("ts_tbeta(x, 3)")
    for t in range(2, X3.shape[0]):
        for n in range(2):
            xv = X3[t - 2 : t + 1, n, 0]
            valid = np.isfinite(xv)
            if valid.sum() < 3:
                continue
            xs = xv[valid]
            time_index = np.arange(1, 4, dtype=np.float64)[valid]
            expected = np.sum((xs - xs.mean()) * (time_index - time_index.mean())) / np.sum(
                (time_index - time_index.mean()) ** 2
            )
            np.testing.assert_allclose(result[t, n, 0], expected)


def test_ts_rankcorr_matches_spearman() -> None:
    """验证滚动秩相关等于窗口内中位秩的 Pearson 相关。"""
    result = _compute("ts_rankcorr(x, y, 3)")
    for t in range(2, X3.shape[0]):
        for n in range(2):
            xv = X3[t - 2 : t + 1, n, 0]
            yv = Y2[t - 2 : t + 1, n]
            valid = np.isfinite(xv) & np.isfinite(yv)
            if valid.sum() < 3:
                continue
            xs, ys = xv[valid], yv[valid]
            rx = np.argsort(np.argsort(xs)).astype(float) + 1.0
            ry = np.argsort(np.argsort(ys)).astype(float) + 1.0
            np.testing.assert_allclose(
                result[t, n, 0], np.corrcoef(rx, ry)[0, 1]
            )


def test_ts_split_mean_selects_top_by_second_input() -> None:
    """验证按第二输入截取头部样本后的均值。"""
    result = _compute("ts_split_mean(x, y, 4, 2)")
    for t in range(3, X3.shape[0]):
        for n in range(2):
            xv = X3[t - 3 : t + 1, n, 0]
            yv = Y2[t - 3 : t + 1, n]
            valid = np.isfinite(xv) & np.isfinite(yv)
            if valid.sum() < 4:
                continue
            xs, ys = xv[valid], yv[valid]
            threshold = np.sort(ys)[-2]
            np.testing.assert_allclose(result[t, n, 0], xs[ys >= threshold].mean())


def test_ts_ewm_mean_matches_weighted_mean() -> None:
    """验证指数衰减加权均值。"""
    halflife = 2.0
    result = _compute(f"ts_ewm_mean(x, {halflife}, 4)")
    for t in range(3, X3.shape[0]):
        for n in range(2):
            xv = X3[t - 3 : t + 1, n, 0]
            valid = np.isfinite(xv)
            if valid.sum() < 4:
                continue
            weights = 0.5 ** ((3 - np.arange(4)) / halflife)
            expected = np.sum(xv * weights) / np.sum(weights)
            np.testing.assert_allclose(result[t, n, 0], expected)


def test_ts_ewm_std_matches_weighted_std() -> None:
    """验证指数衰减加权总体标准差。"""
    halflife = 2.0
    result = _compute(f"ts_ewm_std(x, {halflife}, 4)")
    for t in range(3, X3.shape[0]):
        for n in range(2):
            xv = X3[t - 3 : t + 1, n, 0]
            valid = np.isfinite(xv)
            if valid.sum() < 4:
                continue
            weights = 0.5 ** ((3 - np.arange(4)) / halflife)
            mean = np.sum(xv * weights) / np.sum(weights)
            expected = np.sqrt(np.sum(weights * (xv - mean) ** 2) / np.sum(weights))
            np.testing.assert_allclose(result[t, n, 0], expected)


@pytest.mark.parametrize(
    ("expression", "match"),
    [
        ("ts_quantile(x, 3, quantile=1.5)", "quantile must be in"),
        ("ts_ewm_mean(x, 0, 4)", "halflife must be positive"),
        ("ts_split_mean(x, y, 3, 0)", "top must be positive"),
        ("ts_diff(x, -1)", "periods must be non-negative"),
        ("ts_median(x, 3, min_periods=5)", "min_periods must not exceed window"),
    ],
)
def test_invalid_parameters_fail_at_compile_time(expression, match) -> None:
    """验证非法参数在编译期报错且不触发数据加载。"""
    provider = _provider()
    with pytest.raises(CompileError, match=match):
        BatchFactorEngine(provider).compute(_request(expression))
    assert provider.load_calls == []
