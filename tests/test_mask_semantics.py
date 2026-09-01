"""覆盖掩码三值语义、缺失传播与掩码/编码值运行时校验的测试。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    ComputeRequest,
    DomainSpec,
    MemoryDataProvider,
)
from factor_engine.domain import ValueKind
from factor_engine.formula import FormulaBatch
from factor_engine.model import RuntimeExecutionError
from factor_engine.operators import OperatorSpec, default_operator_registry
from factor_engine.operators.cross_section import (
    _member_reduce_kernel,
    _selector_3d,
    cs_mean,
    group_mean,
    member_demean,
    member_mean,
    member_std,
    member_sum,
    rank,
)
from factor_engine.operators.elementwise import (
    apply_mask,
    equal,
    greater,
    greater_equal,
    less,
    less_equal,
    mask_and,
    mask_not,
    mask_or,
    not_equal,
    where,
)


@pytest.mark.parametrize(
    ("operation", "expected_finite"),
    [
        (greater_equal, 1.0),
        (greater, 1.0),
        (less_equal, 0.0),
        (less, 0.0),
        (equal, 0.0),
        (not_equal, 1.0),
    ],
)
def test_comparisons_preserve_missing_inputs(operation, expected_finite) -> None:
    """验证比较算子在输入含缺失时保留缺失。"""
    result = operation(
        np.array([np.nan, 1.0, 2.0]),
        np.array([0.0, np.nan, 1.0]),
    )

    assert result.dtype == np.float64
    np.testing.assert_allclose(
        result, [np.nan, np.nan, expected_finite], equal_nan=True
    )


def test_mask_and_and_or_implement_complete_three_valued_truth_tables() -> None:
    """验证掩码是与或实现完整的三值真值表。"""
    values = np.array([0.0, 1.0, np.nan])
    left = np.repeat(values, 3)
    right = np.tile(values, 3)

    np.testing.assert_allclose(
        mask_and(left, right),
        [0.0, 0.0, 0.0, 0.0, 1.0, np.nan, 0.0, np.nan, np.nan],
        equal_nan=True,
    )
    np.testing.assert_allclose(
        mask_or(left, right),
        [0.0, 1.0, np.nan, 1.0, 1.0, 1.0, np.nan, 1.0, np.nan],
        equal_nan=True,
    )


def test_mask_logic_supports_variadic_inputs_and_broadcasting() -> None:
    """验证掩码逻辑算子支持变长输入与广播。"""
    np.testing.assert_allclose(
        mask_and(np.array([1.0, 1.0]), 1.0, np.array([np.nan, 0.0])),
        [np.nan, 0.0],
        equal_nan=True,
    )
    np.testing.assert_allclose(
        mask_or(np.array([0.0, 0.0]), 0.0, np.array([np.nan, 1.0])),
        [np.nan, 1.0],
        equal_nan=True,
    )
    np.testing.assert_allclose(
        mask_not(np.array([0.0, 1.0, np.nan])),
        [1.0, 0.0, np.nan],
        equal_nan=True,
    )


def test_where_and_apply_mask_propagate_missing_conditions() -> None:
    """验证 where 与 apply_mask 对缺失条件传播缺失。"""
    mask = np.array([1.0, 0.0, np.nan])
    values = np.array([10.0, 20.0, 30.0])

    np.testing.assert_allclose(
        where(mask, values, -values), [10.0, -20.0, np.nan], equal_nan=True
    )
    np.testing.assert_allclose(
        apply_mask(values, mask), [10.0, np.nan, np.nan], equal_nan=True
    )


def test_cross_section_missing_sample_uses_false_selection_semantics() -> None:
    """验证截面算子把缺失样本掩码按未选中处理。"""
    values = np.array([[[1.0], [2.0], [3.0]]])
    sample_mask = np.array([[[1.0], [np.nan], [1.0]]])

    np.testing.assert_allclose(
        cs_mean(values, sample_mask),
        np.array([[[2.0]]]),
        equal_nan=True,
    )
    np.testing.assert_allclose(
        rank(values, sample_mask),
        np.array([[[0.5], [np.nan], [1.0]]]),
        equal_nan=True,
    )
    np.testing.assert_allclose(
        group_mean(values, np.ones((1, 3, 1)), sample_mask),
        np.array([[[2.0], [np.nan], [2.0]]]),
        equal_nan=True,
    )


def test_member_missing_is_treated_as_non_member() -> None:
    """验证成员掩码缺失被视为非成员。"""
    values = np.array([[[1.0], [100.0], [3.0]]])
    member = np.array([[[1.0], [np.nan], [1.0]]])

    np.testing.assert_allclose(
        member_mean(values, member),
        np.array([[[2.0]]]),
        equal_nan=True,
    )


def test_member_reduce_numba_kernel_supports_weighted_multistep_views() -> None:
    """验证成员归约 numba 内核支持加权多 step 视图。"""
    values = np.array([[[1.0, 10.0], [3.0, 20.0], [100.0, 30.0]]])
    member = np.array([[[1.0], [1.0], [0.0]]])
    weight = np.array([[[1.0], [3.0], [1.0]]])

    np.testing.assert_allclose(
        member_mean(values, member, weight=weight),
        np.array([[[2.5, 17.5]]]),
    )
    np.testing.assert_allclose(
        member_sum(values, member, weight=weight),
        np.array([[[10.0, 70.0]]]),
    )
    np.testing.assert_allclose(
        member_std(values, member, weight=weight),
        np.array([[[np.sqrt(0.75), np.sqrt(18.75)]]]),
    )
    assert _member_reduce_kernel.signatures


def test_member_selector_broadcast_is_a_zero_stride_view() -> None:
    """验证成员选择器广播为零步长共享内存视图。"""
    member = np.array([[[1.0], [0.0], [1.0]]])

    aligned = _selector_3d(member, (1, 3, 4), name="member mask")

    assert aligned.shape == (1, 3, 4)
    assert np.shares_memory(member, aligned)
    assert aligned.strides[2] == 0
    assert not aligned.flags.writeable


def test_member_kernels_accept_noncontiguous_values_and_keep_transform_shape() -> None:
    """验证成员内核接受非连续数组并保持变换形状。"""
    base = np.arange(12, dtype=np.float64).reshape(1, 3, 4)
    values = base[:, :, ::2]
    member = np.array([[[1.0], [0.0], [1.0]]])

    reduced = member_mean(values, member)
    transformed = member_demean(values, member)

    assert not values.flags.c_contiguous
    np.testing.assert_allclose(
        reduced,
        ((values[:, :1, :] + values[:, 2:3, :]) / 2.0),
    )
    expected = np.full(values.shape, np.nan)
    expected[:, 0, :] = values[:, 0, :] - reduced[:, 0, :]
    expected[:, 2, :] = values[:, 2, :] - reduced[:, 0, :]
    np.testing.assert_allclose(transformed, expected, equal_nan=True)


def test_batch_runtime_preserves_comparison_missing_and_float64_mask_dtype() -> None:
    """验证运行时保留比较缺失并保持 float64 掩码类型。"""
    provider = MemoryDataProvider(
        dates=["20240102", "20240103"],
        asset_codes={"stk": [1, 2]},
        data={"stk.1d.x": np.array([[np.nan, 1.0], [2.0, -1.0]])},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={
                "comparison": "factor = x > 0",
                "masked": "positive = x > 0\nfactor = apply_mask(x, positive)",
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    assert result.arrays["comparison"].dtype == np.float64
    np.testing.assert_allclose(
        result.arrays["comparison"][:, :, 0],
        [[np.nan, 1.0], [1.0, 0.0]],
        equal_nan=True,
    )
    np.testing.assert_allclose(
        result.arrays["masked"][:, :, 0],
        [[np.nan, 1.0], [2.0, np.nan]],
        equal_nan=True,
    )


def test_batch_runtime_rejects_invalid_mask_operator_results() -> None:
    """验证运行时拒绝取值非法的掩码算子结果。"""
    provider = MemoryDataProvider(
        dates=["20240102"],
        asset_codes={"stk": [1]},
        data={"stk.1d.x": np.array([[1.0]])},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240102", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={"invalid": "factor = invalid_mask(x)"},
        ),
    )
    operators = default_operator_registry()
    operators["invalid_mask"] = OperatorSpec(
        "invalid_mask",
        lambda x: np.full_like(x, 2.0),
        (ValueKind.NUMERIC,),
        ValueKind.MASK,
    )

    with pytest.raises(RuntimeExecutionError, match="only 0.0, 1.0, or NaN"):
        BatchFactorEngine(provider, operators=operators).compute(request)


def test_batch_runtime_rejects_non_integer_code_operator_results() -> None:
    """验证运行时拒绝非整数取值的编码类算子结果。"""
    provider = MemoryDataProvider(
        dates=["20240102"],
        asset_codes={"stk": [1]},
        data={"stk.1d.x": np.array([[1.0]])},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240102", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="x = source('stk.1d.x')",
            formulas={"invalid": "factor = invalid_code(x)"},
        ),
    )
    operators = default_operator_registry()
    operators["invalid_code"] = OperatorSpec(
        "invalid_code",
        lambda x: np.full_like(x, 1.5),
        (ValueKind.NUMERIC,),
        ValueKind.CODE,
    )

    with pytest.raises(RuntimeExecutionError, match="contains non-integer values"):
        BatchFactorEngine(provider, operators=operators).compute(request)
