"""覆盖频率与 step 对齐规则（广播、显式对齐、资产选取、截面归约）的测试。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    CompileError,
    ComputeRequest,
    DomainError,
    DomainSpec,
    FormulaBatch,
    InputSpec,
    MemoryDataProvider,
    OperatorTerm,
    ValueKind,
)
from factor_engine.operators.alignment import select_by_pos


DATES = ["20240102", "20240103"]
STOCKS = [11, 22, 33]


def _request(
    *,
    common_inputs: str,
    formula: str,
    target_freq: str,
    target_step_count: int,
    asset_scope=None,
) -> ComputeRequest:
    """构造指定目标频率与 step 数的单公式计算请求。"""
    return ComputeRequest(
        DomainSpec(
            DATES[0],
            DATES[-1],
            asset_scope or {"stk": "all"},
            "stk",
            target_freq,
            target_step_count,
        ),
        FormulaBatch.from_text(
            common_inputs=common_inputs, formulas={"alpha": formula}
        ),
    )


def test_daily_singleton_and_daily_multistep_use_numpy_broadcasting() -> None:
    """验证日频单 step 与日频多 step 用 NumPy 广播对齐且无隐式算子。"""
    quote = np.arange(6, dtype=np.float64).reshape(2, 3, 1)
    fundamental = np.arange(24, dtype=np.float64).reshape(2, 3, 4)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.1d.quote": quote, "stk.1d.fund": fundamental},
        input_specs={
            "stk.1d.quote": InputSpec("stk", "1d", 1),
            "stk.1d.fund": InputSpec("stk", "1d", 4),
        },
    )
    request = _request(
        common_inputs=(
            "quote = source('stk.1d.quote')\n"
            "fund = source('stk.1d.fund')"
        ),
        formula="factor = quote + fund",
        target_freq="1d",
        target_step_count=4,
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(result.arrays["alpha"], quote + fundamental)
    assert not any(
        isinstance(term, OperatorTerm)
        and term.operator_name
        in {"__broadcast_steps", "__ffill_steps", "__index_broadcast"}
        for term in result.plan.terms.values()
    )


def test_same_shape_different_frequencies_compute_positionally() -> None:
    """验证 shape 相同但业务坐标不同的输入按位置计算（设计§3）。"""
    fund = np.arange(24, dtype=np.float64).reshape(2, 3, 4)
    price = np.ones((2, 3, 4), dtype=np.float64)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.1d.fund": fund, "stk.5min.price": price},
        input_specs={
            "stk.1d.fund": InputSpec("stk", "1d", 4),
            "stk.5min.price": InputSpec("stk", "5min", 4),
        },
    )
    request = _request(
        common_inputs=(
            "fund = source('stk.1d.fund')\n"
            "price = source('stk.5min.price')"
        ),
        formula="factor = fund + price",
        target_freq="5min",
        target_step_count=4,
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(result.arrays["alpha"], fund + price)


def test_incompatible_step_dimensions_fail_at_compile_time() -> None:
    """验证 step 维不可广播时在编译期报普通 shape 错误。"""
    values = np.ones((2, 3, 4), dtype=np.float64)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.5min.x": values, "stk.15min.y": np.ones((2, 3, 3))},
        input_specs={
            "stk.5min.x": InputSpec("stk", "5min", 4),
            "stk.15min.y": InputSpec("stk", "15min", 3),
        },
    )
    request = _request(
        common_inputs="x = source('stk.5min.x')\ny = source('stk.15min.y')",
        formula="factor = x + y",
        target_freq="5min",
        target_step_count=4,
    )

    with pytest.raises(DomainError, match="Incompatible step dimensions"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


def test_coarse_source_cannot_broadcast_to_finer_step_output() -> None:
    """验证粗频源的 step 维无法广播到更细的输出域时在编译期报错。"""
    values = np.ones((2, 3, 4), dtype=np.float64)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.60min.x": values},
    )
    request = _request(
        common_inputs="x = source('stk.60min.x')",
        formula="factor = x",
        target_freq="30min",
        target_step_count=8,
    )

    with pytest.raises(DomainError, match="cannot broadcast to target"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


@pytest.mark.parametrize(
    "formula",
    ["factor = resample(x, '1d')", "factor = align_frequency(x, '1min')"],
)
def test_frequency_conversion_requires_an_explicit_method(formula: str) -> None:
    """验证频率转换算子必须显式指定 method 参数。"""
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.60min.x": np.ones((2, 3, 4), dtype=np.float64)},
    )
    request = _request(
        common_inputs="x = source('stk.60min.x')",
        formula=formula,
        target_freq="1d",
        target_step_count=1,
    )

    with pytest.raises(CompileError, match="requires an explicit method"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


def test_resample_then_align_frequency_supports_an_explicit_intermediate_domain() -> (
    None
):
    """验证先 resample 再 align_frequency 支持显式中间域。"""
    values = np.arange(2 * 3 * 237, dtype=np.float64).reshape(2, 3, 237)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.1min.x": values},
    )
    request = _request(
        common_inputs="x = source('stk.1min.x')",
        formula=(
            "coarse = resample(x, '15min', method='mean')\n"
            "factor = align_frequency(coarse, '5min', method='ffill')"
        ),
        target_freq="5min",
        target_step_count=48,
    )

    result = BatchFactorEngine(provider).compute(request)

    assert result.arrays["alpha"].shape == (2, 3, 48)
    names = {
        term.operator_name
        for term in result.plan.terms.values()
        if isinstance(term, OperatorTerm)
    }
    assert {"resample", "align_frequency"} <= names


def test_asset_selection_is_a_singleton_view_and_broadcasts_later() -> None:
    """验证资产选取是单资产视图并在后续按需广播。"""
    values = np.arange(12, dtype=np.float64).reshape(2, 3, 2)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.1d.x": values},
        input_specs={"stk.1d.x": InputSpec("stk", "1d", 2)},
    )
    request = _request(
        common_inputs="x = source('stk.1d.x')",
        formula="selected = select_asset(x, 22)\nfactor = x - selected",
        target_freq="1d",
        target_step_count=2,
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(
        result.arrays["alpha"], values - values[:, 1:2, :]
    )
    selected_term = next(
        term
        for term in result.plan.terms.values()
        if isinstance(term, OperatorTerm) and term.operator_name == "select_by_pos"
    )
    assert selected_term.domain is not None
    assert selected_term.domain.asset_count == 1
    assert selected_term.domain.asset_type is None
    selected = select_by_pos(values, 1, axis=1, keepdims=True)
    assert selected.shape == (2, 1, 2)
    assert np.shares_memory(values, selected)


def test_cross_section_reduce_stays_singleton_until_output_boundary() -> None:
    """验证截面归约保持单资产视图直到输出边界。"""
    values = np.arange(6, dtype=np.float64).reshape(2, 3, 1)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.1d.x": values},
    )
    request = _request(
        common_inputs="x = source('stk.1d.x')",
        formula="factor = cs_mean(x)",
        target_freq="1d",
        target_step_count=1,
    )
    stream = BatchFactorEngine(provider).stream(request)

    chunk = next(iter(stream))

    expected = np.broadcast_to(values.mean(axis=1, keepdims=True), values.shape)
    np.testing.assert_array_equal(chunk.values, expected)
    assert not chunk.values.flags.writeable
    assert chunk.values.strides[1] == 0
    reduce_term = next(
        term
        for term in stream.plan.terms.values()
        if isinstance(term, OperatorTerm) and term.operator_name == "cs_mean"
    )
    assert reduce_term.domain is not None
    assert reduce_term.domain.asset_count == 1
    assert reduce_term.domain.asset_type is None


def test_member_reduce_returns_anonymous_singleton() -> None:
    """验证成员归约返回无资产标识的单例结果。"""
    values = np.array([[[1.0], [100.0], [3.0]], [[2.0], [4.0], [6.0]]])
    member = np.array([[[1.0], [0.0], [1.0]], [[0.0], [1.0], [1.0]]])
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS},
        data={"stk.1d.x": values, "stk.1d.member": member},
        input_specs={
            "stk.1d.x": InputSpec("stk", "1d", 1),
            "stk.1d.member": InputSpec(
                "stk", "1d", 1, value_kind=ValueKind.MASK
            ),
        },
    )
    request = _request(
        common_inputs=(
            "x = source('stk.1d.x')\n"
            "member = source('stk.1d.member')"
        ),
        formula="factor = member_mean(x, member)",
        target_freq="1d",
        target_step_count=1,
    )

    result = BatchFactorEngine(provider).compute(request)

    expected = np.array([[[2.0]], [[5.0]]])
    np.testing.assert_array_equal(
        result.arrays["alpha"], np.broadcast_to(expected, values.shape)
    )
    term = next(
        term
        for term in result.plan.terms.values()
        if isinstance(term, OperatorTerm) and term.operator_name == "member_mean"
    )
    assert term.domain is not None
    assert term.domain.asset_count == 1
    assert term.domain.asset_type is None


def test_group_codes_can_vary_by_step_and_return_the_full_asset_axis() -> None:
    """验证分组编码可逐 step 变化并返回完整资产轴结果。"""
    values = np.array([[[1.0, 10.0], [3.0, 20.0], [10.0, 30.0]]])
    groups = np.array([[[1.0, 1.0], [1.0, 2.0], [2.0, 2.0]]])
    provider = MemoryDataProvider(
        dates=DATES[:1],
        asset_codes={"stk": STOCKS},
        data={"stk.1d.x": values, "stk.1d.group": groups},
        input_specs={
            "stk.1d.x": InputSpec("stk", "1d", 2),
            "stk.1d.group": InputSpec(
                "stk", "1d", 2, value_kind=ValueKind.CODE
            ),
        },
    )
    request = ComputeRequest(
        DomainSpec(DATES[0], DATES[0], {"stk": "all"}, "stk", "1d", 2),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')\ng = source('stk.1d.group')",
            formulas={"alpha": "factor = group_mean(x, g)"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(
        result.arrays["alpha"],
        np.array([[[2.0, 10.0], [2.0, 25.0], [10.0, 25.0]]]),
    )


def test_asset_dimension_mismatch_reports_both_asset_types() -> None:
    """验证跨资产误用的编译诊断包含两侧资产类型与显式转换建议（设计§3.1）。"""
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS, "cb": [101, 102]},
        data={
            "stk.1d.x": np.ones((2, 3, 1)),
            "cb.1d.y": np.ones((2, 2, 1)),
        },
    )
    request = ComputeRequest(
        DomainSpec(
            DATES[0],
            DATES[-1],
            {"stk": "all", "cb": "all"},
            "stk",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')\ny = source('cb.1d.y')",
            formulas={"alpha": "factor = x + y"},
        ),
    )

    with pytest.raises(
        DomainError,
        match=(
            r"Asset dimension mismatch: stk\(N=3\) cannot broadcast with "
            r"cb\(N=2\); use an explicit mapping, selection, or reduction operator"
        ),
    ):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


def test_same_asset_count_different_asset_types_compute_positionally() -> None:
    """验证资产数相同但资产类型不同的输入按位置计算。"""
    stock = np.arange(6, dtype=np.float64).reshape(2, 3, 1)[:, :2]
    bond = np.ones((2, 2, 1), dtype=np.float64)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS, "cb": [101, 102]},
        data={"stk.1d.x": stock, "cb.1d.y": bond},
        input_specs={
            "stk.1d.x": InputSpec("stk", "1d", 1),
            "cb.1d.y": InputSpec("cb", "1d", 1),
        },
    )
    request = ComputeRequest(
        DomainSpec(
            DATES[0],
            DATES[-1],
            {"stk": [11, 22], "cb": "all"},
            "stk",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')\ny = source('cb.1d.y')",
            formulas={"alpha": "factor = x + y"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(result.arrays["alpha"], stock + bond)


def test_singleton_asset_broadcast_ignores_asset_type() -> None:
    """验证任意一侧 N=1 时允许广播，即使资产类型不同。"""
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS, "idx": [300]},
        data={
            "stk.1d.x": np.arange(6, dtype=np.float64).reshape(2, 3, 1),
            "idx.1d.y": np.array([[2.0], [3.0]]),
        },
    )
    request = ComputeRequest(
        DomainSpec(
            DATES[0],
            DATES[-1],
            {"stk": "all", "idx": "all"},
            "stk",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')\ny = source('idx.1d.y')",
            formulas={"alpha": "factor = x + y"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    expected = np.arange(6, dtype=np.float64).reshape(2, 3, 1) + [[[2.0]], [[3.0]]]
    np.testing.assert_array_equal(result.arrays["alpha"], expected)


def test_ambiguous_asset_provenance_falls_back_to_plain_shape_error() -> None:
    """验证复合表达式无法确定唯一资产来源时退回普通 shape 错误（设计§3.1）。"""
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": STOCKS, "cb": [101, 102, 103], "idx": [300, 500]},
        data={
            "stk.1d.x": np.ones((2, 3, 1)),
            "cb.1d.y": np.ones((2, 3, 1)),
            "idx.1d.z": np.ones((2, 2, 1)),
        },
        input_specs={
            "stk.1d.x": InputSpec("stk", "1d", 1),
            "cb.1d.y": InputSpec("cb", "1d", 1),
            "idx.1d.z": InputSpec("idx", "1d", 1),
        },
    )
    # x + y 的 N 相同但资产来源混合，hint 失效后再与 idx 运算只报 shape 错误。
    request = ComputeRequest(
        DomainSpec(
            DATES[0],
            DATES[-1],
            {"stk": "all", "cb": "all", "idx": "all"},
            "stk",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs=(
                "x = source('stk.1d.x')\n"
                "y = source('cb.1d.y')\n"
                "z = source('idx.1d.z')"
            ),
            formulas={"alpha": "factor = x + y + z"},
        ),
    )

    with pytest.raises(DomainError, match="Incompatible asset dimensions"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []
