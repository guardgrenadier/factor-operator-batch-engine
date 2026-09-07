"""覆盖时间契约（历史回看、负延迟、轴约束）在编译期校验的测试。"""

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


DATES = ["20240102", "20240103", "20240104", "20240105", "20240108"]


def _provider(values=None) -> MemoryDataProvider:
    """构造含可选自定义值的日频内存数据提供方。"""
    data = (
        np.arange(1, 11, dtype=np.float64).reshape(5, 2)
        if values is None
        else np.asarray(values, dtype=np.float64)
    )
    return MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": [1, 2]},
        data={"stk.1d.x": data},
    )


def _request(expression: str) -> ComputeRequest:
    """构造以给定表达式为因子输出的计算请求。"""
    return ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={"alpha": f"factor = {expression}"},
        ),
    )


@pytest.mark.parametrize(
    "expression",
    [
        "delay(x, periods=-1)",
        "delay(x, periods=-1, axis=1)",
        "delay(x, periods=-1, axis=2)",
        "step_delay(x, periods=-1)",
        "step_diff(x, periods=-1)",
        "step_pct_change(x, periods=-1)",
    ],
)
def test_negative_periods_fail_during_compilation_before_loading(expression) -> None:
    """验证负延迟在编译期报错且不触发数据加载。"""
    provider = _provider()

    with pytest.raises(
        CompileError,
        match="periods must be non-negative; future reads are not supported",
    ):
        BatchFactorEngine(provider).compute(_request(expression))

    assert provider.load_calls == []


@pytest.mark.parametrize(
    "expression",
    [
        "delay(x, periods=1, axis=-3)",
        "delay(x, periods=1, axis=-2)",
        "delay(x, periods=1, axis=-1)",
        "delay(x, periods=1, axis=3)",
        "ffill(x, axis=-3, limit=2)",
        "ts_mean(x, 2, axis=-3)",
        "select_by_pos(x, 0, axis=-1, keepdims=True)",
    ],
)
def test_unsupported_axes_fail_during_compilation_before_loading(expression) -> None:
    """验证不支持的轴取值在编译期报错且不触发数据加载。"""
    provider = _provider()

    with pytest.raises(CompileError, match="axis must be one of 0, 1, or 2"):
        BatchFactorEngine(provider).compute(_request(expression))

    assert provider.load_calls == []


def test_date_delay_lookback_is_exact_and_chunk_independent() -> None:
    """验证日期延迟的历史回看精确且不依赖分块划分。"""
    request = _request("delay(x, periods=2, axis=0)")
    whole = BatchFactorEngine(_provider()).compute(request)
    chunked = BatchFactorEngine(_provider()).compute(
        request, options=ExecutionOptions(chunk_size=2)
    )

    assert whole.plan.job_lookback == 2
    np.testing.assert_allclose(
        whole.arrays["alpha"], chunked.arrays["alpha"], equal_nan=True
    )
    np.testing.assert_allclose(
        whole.arrays["alpha"][:, :, 0],
        [[np.nan, np.nan], [np.nan, np.nan], [1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
        equal_nan=True,
    )


def test_date_ffill_lookback_is_exact_and_chunk_independent() -> None:
    """验证日期前向填充的历史回看等于 limit 且不依赖分块。"""
    values = [
        [1.0, 10.0],
        [np.nan, np.nan],
        [np.nan, 30.0],
        [4.0, np.nan],
        [np.nan, np.nan],
    ]
    request = _request("ffill(x, axis=0, limit=2)")
    whole = BatchFactorEngine(_provider(values)).compute(request)
    chunked = BatchFactorEngine(_provider(values)).compute(
        request, options=ExecutionOptions(chunk_size=2)
    )

    assert whole.plan.job_lookback == 2
    np.testing.assert_allclose(
        whole.arrays["alpha"], chunked.arrays["alpha"], equal_nan=True
    )


@pytest.mark.parametrize("axis", [1, 2])
def test_non_date_delay_does_not_add_date_lookback(axis) -> None:
    """验证非日期轴的延迟不会引入日期维历史回看。"""
    job = BatchFactorEngine(_provider()).compile(
        _request(f"delay(x, periods=1, axis={axis})")
    )

    assert job.plan.job_lookback == 0


@pytest.mark.parametrize("periods", [1.0, "1", True])
def test_periods_are_canonicalized_and_validated_during_compilation(periods) -> None:
    """验证非整数 periods 在编译期被规范化与拒绝。"""
    provider = _provider()

    with pytest.raises(CompileError, match="periods must be an integer"):
        BatchFactorEngine(provider).compile(_request(f"delay(x, periods={periods!r})"))

    assert provider.load_calls == []
