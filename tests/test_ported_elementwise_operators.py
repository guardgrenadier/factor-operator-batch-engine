"""覆盖移植的逐元素算子的数值语义、缺失传播与编译期校验。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    CompileError,
    ComputeRequest,
    DomainSpec,
    MemoryDataProvider,
)
from factor_engine.formula import FormulaBatch


DATES = ["20240102", "20240103"]


def _provider(x, y=None) -> MemoryDataProvider:
    """构造含一至两个数值输入的内存数据提供方。"""
    data = {"stk.1d.x": np.asarray(x, dtype=np.float64)}
    if y is not None:
        data["stk.1d.y"] = np.asarray(y, dtype=np.float64)
    return MemoryDataProvider(dates=DATES, asset_codes={"stk": [1, 2]}, data=data)


def _compute(provider, expression, formula_id="alpha"):
    """以给定表达式为因子输出执行完整计算。"""
    common = "x = source('stk.1d.x')"
    if "y" in expression.split("=")[-1]:
        common += "\ny = source('stk.1d.y')"
    request = ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=common, formulas={formula_id: f"factor = {expression}"}
        ),
    )
    return BatchFactorEngine(provider).compute(request).arrays[formula_id]


@pytest.fixture
def xy():
    """构造含缺失值的双输入样本。"""
    x = np.array([[[1.0], [-2.0]], [[np.nan], [4.0]]])
    y = np.array([[[2.0], [3.0]], [[1.0], [np.nan]]])
    return x, y


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("sign(x)", [[[1.0], [-1.0]], [[np.nan], [1.0]]]),
        ("power2(x)", [[[1.0], [4.0]], [[np.nan], [16.0]]]),
        ("power3(x)", [[[1.0], [-8.0]], [[np.nan], [64.0]]]),
        ("curt(x)", [[[1.0], [-(2.0 ** (1 / 3))]], [[np.nan], [4.0 ** (1 / 3)]]]),
        ("inv(x)", [[[1.0], [-0.5]], [[np.nan], [0.25]]]),
        ("exp(x)", np.exp([[[1.0], [-2.0]], [[np.nan], [4.0]]]).tolist()),
        ("protected_sqrt(x)", [[[1.0], [-np.sqrt(2.0)]], [[np.nan], [2.0]]]),
        ("protected_log(x)", [[[np.log(2.0)], [-np.log(3.0)]], [[np.nan], [np.log(5.0)]]]),
        ("sin(x)", np.sin([[[1.0], [-2.0]], [[np.nan], [4.0]]]).tolist()),
        ("cos(x)", np.cos([[[1.0], [-2.0]], [[np.nan], [4.0]]]).tolist()),
        ("one(x)", [[[1.0], [1.0]], [[np.nan], [1.0]]]),
        ("power(x, 2)", [[[1.0], [4.0]], [[np.nan], [16.0]]]),
        ("series_min(x, y)", [[[1.0], [-2.0]], [[np.nan], [np.nan]]]),
        ("series_max(x, y)", [[[2.0], [3.0]], [[np.nan], [np.nan]]]),
        ("hardsigmoid(x)", [[[1.0], [0.0]], [[np.nan], [1.0]]]),
        ("leakyrelu(x)", [[[1.0], [-0.2]], [[np.nan], [4.0]]]),
        ("leakyrelu(x, alpha=0.5)", [[[1.0], [-1.0]], [[np.nan], [4.0]]]),
    ],
)
def test_elementwise_operators_match_numpy(xy, expression, expected) -> None:
    """验证逐元素算子的数值结果与手工 NumPy 计算一致。"""
    x, y = xy
    result = _compute(_provider(x, y), expression)
    np.testing.assert_allclose(result, np.asarray(expected), equal_nan=True)


def test_sigmoid_and_tan_match_numpy(xy) -> None:
    """验证 sigmoid 与 tan 的数值与饱和行为。"""
    x, _ = xy
    np.testing.assert_allclose(
        _compute(_provider(x), "sigmoid(x)"),
        1.0 / (1.0 + np.exp(-x)),
        equal_nan=True,
    )
    np.testing.assert_allclose(
        _compute(_provider(x), "tan(x)"), np.tan(x), equal_nan=True
    )
    extreme = np.array([[[1000.0], [-1000.0]], [[0.0], [1.0]]])
    np.testing.assert_allclose(
        _compute(_provider(extreme), "sigmoid(x)"),
        [[[1.0], [0.0]], [[0.5], [1.0 / (1.0 + np.exp(-1.0))]]],
    )


def test_gelu_matches_tanh_approximation(xy) -> None:
    """验证 GELU 与 tanh 近似公式一致。"""
    x, _ = xy
    expected = x * 0.5 * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * x**3)))
    np.testing.assert_allclose(
        _compute(_provider(x), "gelu(x)"), expected, equal_nan=True
    )


def test_if_then_else_uses_tristate_condition(xy) -> None:
    """验证 if_then_else 按三值比较选择，条件缺失输出缺失。"""
    x, y = xy
    result = _compute(_provider(x, y), "if_then_else(x, y, 1.0, 0.0)")
    np.testing.assert_allclose(
        result, [[[0.0], [0.0]], [[np.nan], [np.nan]]], equal_nan=True
    )


def test_elementwise_unknown_argument_fails_at_compile_time(xy) -> None:
    """验证未知配置参数在编译期报错。"""
    x, _ = xy
    with pytest.raises(CompileError, match="Invalid arguments"):
        _compute(_provider(x), "leakyrelu(x, beta=0.2)")
