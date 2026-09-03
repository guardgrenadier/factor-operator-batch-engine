"""算子注册表：集中声明各算子的值类型契约、Lookback 与布局规则。"""

from __future__ import annotations

import operator
from typing import Any, Callable

from ..domain import ValueKind
from .alignment import lookup_by_col, select_by_pos
from .cross_section import (
    cs_beta,
    cs_corr,
    cs_count,
    cs_cov,
    cs_cumprod,
    cs_cv,
    cs_entropy,
    cs_gauss_rank,
    cs_kurt,
    cs_mad,
    cs_max,
    cs_mean,
    cs_median,
    cs_min,
    cs_min_max_scale,
    cs_quantile,
    cs_rel_entropy,
    cs_skew,
    cs_std,
    cs_sum,
    cs_var,
    cs_zscore,
    group_demean,
    group_mean,
    group_std,
    group_sum,
    group_zscore,
    location,
    member_demean,
    member_mean,
    member_std,
    member_sum,
    member_zscore,
    neutralize,
    ols,
    rank,
    rank_add,
    rank_div,
    rank_mul,
    rank_sub,
    umr,
    winsorize,
)
from .layout_rules import (
    asset_reduce_layout,
    get_step_layout,
    location_layout,
    lookup_by_col_layout,
    select_by_pos_layout,
    slice_step_layout,
    step_reduce_layout,
)
from .elementwise import (
    OperatorSpec,
    VariadicInput,
    abs_val,
    add,
    apply_mask,
    cos,
    curt,
    divide,
    equal,
    exp,
    gelu,
    greater,
    greater_equal,
    hardsigmoid,
    if_then_else,
    inv,
    leakyrelu,
    less,
    less_equal,
    ln,
    log,
    log10,
    mask_and,
    mask_not,
    mask_or,
    multiply,
    neg,
    not_equal,
    one,
    power,
    power2,
    power3,
    protected_log,
    protected_sqrt,
    series_max,
    series_min,
    sigmoid,
    sign,
    sin,
    sqrt,
    subtract,
    tan,
    where,
)
from .timeseries import (
    align_frequency,
    delay,
    ffill,
    get_step,
    intraday_by_step_mean,
    intraday_by_step_std,
    intraday_flat_mean,
    intraday_flat_std,
    resample,
    slice_step,
    step_corr,
    step_delay,
    step_diff,
    step_first,
    step_kurtosis,
    step_last,
    step_max,
    step_mean,
    step_min,
    step_pct_change,
    step_std,
    step_sum,
    ts_argmax,
    ts_argmin,
    ts_beta,
    ts_corr,
    ts_cov,
    ts_cumprod,
    ts_diff,
    ts_ewm_mean,
    ts_ewm_std,
    ts_ffill,
    ts_max,
    ts_max_to_min,
    ts_mean,
    ts_median,
    ts_min,
    ts_pct_change,
    ts_quantile,
    ts_rank,
    ts_rankcorr,
    ts_split_corr,
    ts_split_mean,
    ts_split_std,
    ts_std,
    ts_sum,
    ts_tbeta,
    ts_var,
)


def _date_delay_lookback(params: dict[str, Any]) -> int:
    """根据日期轴延迟参数计算所需回看长度。"""
    periods = params.get("periods", 1)
    if params.get("axis", 0) != 0:
        return 0
    return periods


def _nonnegative_periods_lookback(params: dict[str, Any]) -> int:
    """step delay 家族不增加日期回看。"""
    return 0


def _axis_only_lookback(params: dict[str, Any]) -> int:
    """不依赖日期历史的算子无回看。"""
    return 0


def _date_window_lookback(params: dict[str, Any]) -> int:
    """根据日期轴窗口大小计算所需回看长度。"""
    if params.get("axis", 0) != 0:
        return 0
    return max(0, int(params.get("window", 5)) - 1)


def _window_lookback(default_window: int):
    """为窗口默认值不同的算子生成日期轴回看函数。"""

    def lookback(params: dict[str, Any]) -> int:
        if params.get("axis", 0) != 0:
            return 0
        return max(0, int(params.get("window", default_window)) - 1)

    return lookback


def _date_ffill_lookback(params: dict[str, Any]) -> int:
    """根据有限前向填充参数计算日期回看长度。"""
    if params.get("axis", 0) != 0:
        return 0
    limit = params.get("limit")
    if limit is None:
        raise ValueError("date-axis ffill requires a finite limit in batch execution")
    return max(0, int(limit))


def _ts_ffill_lookback(params: dict[str, Any]) -> int:
    """根据时序前向填充上限计算日期回看长度。"""
    limit = params.get("limit")
    if limit is None:
        raise ValueError("ts_ffill requires a finite limit in batch execution")
    return max(0, int(limit))


def _intraday_window_lookback(params: dict[str, Any]) -> int:
    """根据日内窗口天数计算日期回看长度。"""
    return max(0, int(params["window_days"]) - 1)


def _winsorize_params(params: dict[str, Any]) -> dict[str, Any]:
    """校验 winsorize 的截断边界落在有序概率区间内并规范为浮点。"""
    lower = float(params.get("lower", 0.01))
    upper = float(params.get("upper", 0.99))
    if not 0.0 <= lower <= upper <= 1.0:
        raise ValueError("winsorize requires 0 <= lower <= upper <= 1")
    return {**params, "lower": lower, "upper": upper}


def _quantile_params(key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """为分位数算子生成参数校验：分位数必须落在 [0, 1] 并规范为浮点。"""

    def validate(params: dict[str, Any]) -> dict[str, Any]:
        if params.get(key) is None:
            return params
        value = float(params[key])
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{key} must be in [0, 1]")
        return {**params, key: value}

    return validate


def _halflife_params(params: dict[str, Any]) -> dict[str, Any]:
    """校验指数加权半衰期为正数并规范为浮点。"""
    if params.get("halflife") is None:
        return params
    halflife = float(params["halflife"])
    if halflife <= 0.0:
        raise ValueError("halflife must be positive")
    return {**params, "halflife": halflife}


def _top_params(params: dict[str, Any]) -> dict[str, Any]:
    """校验分组截取数量为正整数。"""
    if params.get("top") is None:
        return params
    top = params["top"]
    if isinstance(top, bool):
        raise ValueError("top must be an integer")
    try:
        top = int(operator.index(top))
    except TypeError as exc:
        raise ValueError("top must be an integer") from exc
    if top <= 0:
        raise ValueError("top must be positive")
    return {**params, "top": top}


def _cs_corr_params(params: dict[str, Any]) -> dict[str, Any]:
    """校验截面相关系数只使用已实现的方法。"""
    if params.get("method", "pearson") not in ("pearson", "spearman"):
        raise ValueError("cs_corr method must be 'pearson' or 'spearman'")
    return params


def default_operator_registry() -> dict[str, OperatorSpec]:
    """返回 Compiler 和 Runtime 共用的轻量运算符注册表。"""
    # 三种值类型和局部注册 helper 构成后续契约声明的基础。
    numeric = ValueKind.NUMERIC
    mask = ValueKind.MASK
    code = ValueKind.CODE
    sample = (("sample_mask", mask),)
    sample_weight = (*sample, ("weight", numeric))
    registry: dict[str, OperatorSpec] = {}

    def register(
        name: str,
        func: Callable[..., Any],
        inputs: tuple[ValueKind, ...] | VariadicInput,
        output: ValueKind | str = ValueKind.NUMERIC,
        lookback: int | Callable[[dict[str, Any]], int] = 0,
        layout_rule: Callable[..., Any] | None = None,
        optional_inputs: tuple[tuple[str, ValueKind], ...] = (),
        validate_params: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        """向当前注册表加入一项运算符契约。"""
        # 契约完整记录函数、输入输出类型、回看、布局规则和参数校验。
        registry[name] = OperatorSpec(
            name,
            func,
            inputs,
            output,
            lookback,
            layout_rule,
            optional_inputs,
            validate_params,
        )

    # 基础数值运算默认使用 NumPy 广播布局合并规则。
    for name, func in {
        "add": add,
        "subtract": subtract,
        "multiply": multiply,
        "divide": divide,
    }.items():
        register(name, func, (numeric, numeric))

    register("step_corr", step_corr, (numeric, numeric), numeric, 0, step_reduce_layout)

    # 一元数值及截面变换保持输入领域。
    for name, func in {
        "neg": neg,
        "abs": abs_val,
        "abs_val": abs_val,
        "ln": ln,
        "log": log,
        "log10": log10,
        "sqrt": sqrt,
        "cs_zscore": cs_zscore,
        "rank": rank,
    }.items():
        register(name, func, (numeric,), optional_inputs=sample)

    register(
        "winsorize",
        winsorize,
        (numeric,),
        numeric,
        0,
        None,
        sample,
        _winsorize_params,
    )

    # 截面 reduce 将资产轴收缩为 singleton。
    for name, func in {
        "cs_mean": cs_mean,
        "cs_sum": cs_sum,
        "cs_std": cs_std,
    }.items():
        register(name, func, (numeric,), numeric, 0, asset_reduce_layout, sample)

    register("get_step", get_step, (numeric,), numeric, 0, get_step_layout)
    register("slice_step", slice_step, (numeric,), numeric, 0, slice_step_layout)
    register("align_frequency", align_frequency, (numeric,))
    register("resample", resample, (numeric,))

    # step reduce 将第三维收缩为 singleton。
    for name, func in {
        "step_mean": step_mean,
        "step_sum": step_sum,
        "step_std": step_std,
        "step_max": step_max,
        "step_min": step_min,
        "step_first": step_first,
        "step_last": step_last,
        "step_kurtosis": step_kurtosis,
    }.items():
        register(name, func, (numeric,), numeric, 0, step_reduce_layout)

    # 比较和逻辑算子输出三态掩码。
    for name, func in {
        "step_delay": step_delay,
        "step_diff": step_diff,
        "step_pct_change": step_pct_change,
    }.items():
        register(name, func, (numeric,), numeric, _nonnegative_periods_lookback)

    # 日期滚动算子根据窗口参数声明回看长度。
    for name, func in {
        "greater_equal": greater_equal,
        "greater": greater,
        "less_equal": less_equal,
        "less": less,
        "equal": equal,
        "not_equal": not_equal,
    }.items():
        register(name, func, (numeric, numeric), mask)

    register("where", where, (mask, numeric, numeric))
    register("apply_mask", apply_mask, (numeric, mask))
    register("mask_and", mask_and, VariadicInput(mask), mask)
    register("mask_or", mask_or, VariadicInput(mask), mask)
    register("mask_not", mask_not, (mask,), mask)
    register("delay", delay, (numeric,), numeric, _date_delay_lookback)
    register("ffill", ffill, (numeric,), numeric, _date_ffill_lookback)
    register("ts_ffill", ts_ffill, (numeric,), numeric, _ts_ffill_lookback)

    # 分组统计保留完整资产轴，成员 reduce 则返回 singleton 资产轴。
    for name, func in {
        "ts_mean": ts_mean,
        "ts_sum": ts_sum,
        "ts_std": ts_std,
        "ts_min": ts_min,
        "ts_max": ts_max,
    }.items():
        register(name, func, (numeric,), numeric, _date_window_lookback)

    register("neutralize", neutralize, (numeric, numeric), numeric, 0, None, sample)
    register(
        "lookup_by_col",
        lookup_by_col,
        (numeric, code),
        numeric,
        0,
        lookup_by_col_layout,
    )
    register(
        "select_by_pos",
        select_by_pos,
        (numeric,),
        numeric,
        _axis_only_lookback,
        select_by_pos_layout,
    )

    for name, func in {
        "group_mean": group_mean,
        "group_sum": group_sum,
        "group_std": group_std,
        "group_demean": group_demean,
        "group_zscore": group_zscore,
    }.items():
        register(name, func, (numeric, code), numeric, 0, None, sample_weight)

    # 日内跨日统计分别声明是否收缩 step 轴。
    for name, func in {
        "member_mean": member_mean,
        "member_sum": member_sum,
        "member_std": member_std,
    }.items():
        register(
            name, func, (numeric, mask), numeric, 0, asset_reduce_layout, sample_weight
        )

    for name, func in {
        "member_demean": member_demean,
        "member_zscore": member_zscore,
    }.items():
        register(name, func, (numeric, mask), numeric, 0, None, sample_weight)

    for name, func in {
        "intraday_flat_mean": intraday_flat_mean,
        "intraday_flat_std": intraday_flat_std,
    }.items():
        register(
            name,
            func,
            (numeric,),
            numeric,
            _intraday_window_lookback,
            step_reduce_layout,
        )

    for name, func in {
        "intraday_by_step_mean": intraday_by_step_mean,
        "intraday_by_step_std": intraday_by_step_std,
    }.items():
        register(name, func, (numeric,), numeric, _intraday_window_lookback)

    # 移植的逐元素数学函数保持输入布局，无日期回看。
    for name, func in {
        "sign": sign,
        "power2": power2,
        "power3": power3,
        "curt": curt,
        "inv": inv,
        "exp": exp,
        "protected_sqrt": protected_sqrt,
        "protected_log": protected_log,
        "sin": sin,
        "cos": cos,
        "tan": tan,
        "sigmoid": sigmoid,
        "hardsigmoid": hardsigmoid,
        "leakyrelu": leakyrelu,
        "gelu": gelu,
        "one": one,
    }.items():
        register(name, func, (numeric,))

    for name, func in {
        "power": power,
        "series_min": series_min,
        "series_max": series_max,
    }.items():
        register(name, func, (numeric, numeric))

    register("if_then_else", if_then_else, (numeric, numeric, numeric, numeric))

    # 单输入滚动窗口统计在日期轴回看 window - 1。
    for name, func in {
        "ts_median": ts_median,
        "ts_var": ts_var,
        "ts_argmin": ts_argmin,
        "ts_argmax": ts_argmax,
        "ts_rank": ts_rank,
        "ts_cumprod": ts_cumprod,
        "ts_max_to_min": ts_max_to_min,
    }.items():
        register(name, func, (numeric,), numeric, _date_window_lookback)

    register(
        "ts_quantile",
        ts_quantile,
        (numeric,),
        numeric,
        _window_lookback(10),
        None,
        (),
        _quantile_params("quantile"),
    )
    register("ts_diff", ts_diff, (numeric,), numeric, _date_delay_lookback)
    register(
        "ts_pct_change", ts_pct_change, (numeric,), numeric, _date_delay_lookback
    )

    # 双输入滚动窗口统计同样在日期轴回看 window - 1。
    for name, func in {
        "ts_corr": ts_corr,
        "ts_cov": ts_cov,
        "ts_beta": ts_beta,
        "ts_rankcorr": ts_rankcorr,
    }.items():
        register(name, func, (numeric, numeric), numeric, _date_window_lookback)

    for name, func in {
        "ts_split_mean": ts_split_mean,
        "ts_split_std": ts_split_std,
        "ts_split_corr": ts_split_corr,
    }.items():
        register(
            name,
            func,
            (numeric, numeric),
            numeric,
            _date_window_lookback,
            None,
            (),
            _top_params,
        )

    register("ts_tbeta", ts_tbeta, (numeric,), numeric, _date_window_lookback)
    register(
        "ts_ewm_mean",
        ts_ewm_mean,
        (numeric,),
        numeric,
        _window_lookback(10),
        None,
        (),
        _halflife_params,
    )
    register(
        "ts_ewm_std",
        ts_ewm_std,
        (numeric,),
        numeric,
        _window_lookback(10),
        None,
        (),
        _halflife_params,
    )

    # 截面归约把资产轴收缩为 singleton，支持可选样本掩码。
    for name, func in {
        "cs_median": cs_median,
        "cs_var": cs_var,
        "cs_max": cs_max,
        "cs_min": cs_min,
        "cs_skew": cs_skew,
        "cs_kurt": cs_kurt,
        "cs_cv": cs_cv,
        "cs_mad": cs_mad,
        "cs_entropy": cs_entropy,
        "cs_count": cs_count,
        "cs_cumprod": cs_cumprod,
    }.items():
        register(name, func, (numeric,), numeric, 0, asset_reduce_layout, sample)

    register(
        "cs_quantile",
        cs_quantile,
        (numeric,),
        numeric,
        0,
        asset_reduce_layout,
        sample,
        _quantile_params("q"),
    )

    for name, func in {
        "cs_cov": cs_cov,
        "cs_beta": cs_beta,
        "cs_rel_entropy": cs_rel_entropy,
    }.items():
        register(
            name, func, (numeric, numeric), numeric, 0, asset_reduce_layout, sample
        )

    register(
        "cs_corr",
        cs_corr,
        (numeric, numeric),
        numeric,
        0,
        asset_reduce_layout,
        sample,
        _cs_corr_params,
    )

    # 截面变换保留完整资产轴。
    register(
        "cs_min_max_scale",
        cs_min_max_scale,
        (numeric,),
        numeric,
        0,
        None,
        sample,
    )
    register(
        "cs_gauss_rank", cs_gauss_rank, (numeric,), numeric, 0, None, sample
    )
    register("location", location, (numeric,), numeric, 0, location_layout)
    register("umr", umr, (numeric, numeric))
    register("ols", ols, (numeric, numeric), numeric, 0, None, sample)
    for name, func in {
        "rank_add": rank_add,
        "rank_div": rank_div,
        "rank_sub": rank_sub,
        "rank_mul": rank_mul,
    }.items():
        register(name, func, (numeric, numeric))
    return registry
