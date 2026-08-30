"""覆盖算子动态输入（位置/关键字/省略）归一化与值类型校验的测试。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    CompileError,
    ComputeRequest,
    DomainSpec,
    FormulaBatch,
    InputSpec,
    MemoryDataProvider,
    OperatorTerm,
    ValueKind,
)


def _provider() -> MemoryDataProvider:
    """构造含数值、掩码、分组编码与权重输入的内存数据提供方。"""
    return MemoryDataProvider(
        dates=["20240102"],
        asset_codes={"stk": [1, 2, 3]},
        data={
            "stk.1d.x": np.array([[1.0, 100.0, 3.0]]),
            "stk.1d.mask": np.array([[1.0, 0.0, 1.0]]),
            "stk.1d.group": np.ones((1, 3)),
            "stk.1d.weight": np.array([[1.0, 1.0, 3.0]]),
        },
        input_specs={
            "stk.1d.mask": InputSpec("stk", "1d", 1, ValueKind.MASK),
            "stk.1d.group": InputSpec("stk", "1d", 1, ValueKind.CODE),
        },
    )


def _request(formulas: dict[str, str]) -> ComputeRequest:
    """构造引用公共输入的单日频计算请求。"""
    return ComputeRequest(
        DomainSpec("20240102", "20240102", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="""
                x = source("stk.1d.x")
                mask = source("stk.1d.mask")
                group = source("stk.1d.group")
                weight = source("stk.1d.weight")
            """,
            formulas=formulas,
        ),
    )


def test_dynamic_input_positional_and_keyword_forms_share_one_term() -> None:
    """验证位置传参与关键字传参归一化后共用同一算子 Term。"""
    result = BatchFactorEngine(_provider()).compute(
        _request(
            {
                "positional": "factor = rank(x, mask)",
                "keyword": "factor = rank(x, sample_mask=mask)",
                "omitted": "factor = rank(x, sample_mask=None)",
            }
        )
    )

    assert result.plan.outputs["positional"] == result.plan.outputs["keyword"]
    term = result.plan.terms[result.plan.outputs["positional"]]
    assert isinstance(term, OperatorTerm)
    assert term.input_names == (None, "sample_mask")
    np.testing.assert_allclose(
        result.arrays["positional"][0, :, 0],
        [0.5, np.nan, 1.0],
        equal_nan=True,
    )
    np.testing.assert_allclose(result.arrays["omitted"][0, :, 0], [1 / 3, 1.0, 2 / 3])


def test_dynamic_weight_can_skip_optional_sample_mask() -> None:
    """验证可选 sample_mask 可被跳过且关键字参数可重排。"""
    result = BatchFactorEngine(_provider()).compute(
        _request(
            {
                "weighted": "factor = group_mean(x, group, weight=weight)",
                "winsorized": "factor = winsorize(x, mask, 0.0, 1.0)",
                "ordered": (
                    "factor = group_mean(x, group, sample_mask=mask, weight=weight)"
                ),
                "reordered": (
                    "factor = group_mean(x, group, weight=weight, sample_mask=mask)"
                ),
            }
        )
    )

    assert result.plan.outputs["ordered"] == result.plan.outputs["reordered"]
    np.testing.assert_allclose(result.arrays["weighted"], 22.0)
    np.testing.assert_allclose(result.arrays["winsorized"][0, :, 0], [1.0, 3.0, 3.0])


def test_dynamic_input_value_kind_is_checked_at_compile_time() -> None:
    """验证动态输入的值类型不匹配在编译期即报错。"""
    with pytest.raises(CompileError, match="requires mask, got numeric"):
        BatchFactorEngine(_provider()).compile(
            _request({"alpha": "factor = rank(x, sample_mask=weight)"})
        )
