"""覆盖公式批次解析、公共输入与局部绑定、符号引用与作用域规则的测试。"""

from __future__ import annotations

import pytest

from factor_engine.formula import (
    FormulaBatch,
    FormulaParser,
    HelperExpr,
    OperatorExpr,
    SourceRefExpr,
    SymbolBindingError,
    get_hf,
    get_lf,
    operator,
)
from factor_engine.operators import default_operator_registry


def test_formula_batch_binds_common_and_sequential_local_names() -> None:
    """验证公式批次能绑定公共输入与顺序局部绑定。"""
    batch = FormulaBatch.from_text(
        common_inputs='close = get_lf("stk", "ClosePrice")',
        formulas={
            "alpha": """
                mean = ts_mean(close, 2)
                factor = mean / close
            """,
        },
    )

    bound = batch.bind()

    output = bound.outputs["alpha"]
    assert isinstance(output, OperatorExpr)
    assert output.name == "divide"
    assert isinstance(output.args[0], OperatorExpr)
    assert output.args[0].name == "ts_mean"
    assert isinstance(output.args[0].args[0], HelperExpr)


def test_formula_groups_have_independent_local_scopes() -> None:
    """验证各公式之间局部绑定的作用域相互独立。"""
    batch = FormulaBatch.from_text(
        common_inputs='close = source("stk.1d.close")',
        formulas={
            "one": "shared = close + 1",
            "two": "shared = close + 2",
        },
    )

    outputs = batch.bind().outputs

    assert outputs["one"] != outputs["two"]


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("factor = later + 1\nlater = 2", "forward reference 'later'"),
        ("factor = missing + 1", "unknown name 'missing'"),
    ],
)
def test_symbol_errors_include_formula_and_source_position(program, message) -> None:
    """验证符号引用错误信息包含公式标识与源码位置。"""
    batch = FormulaBatch.from_text(formulas={"alpha": program})

    with pytest.raises(SymbolBindingError) as error:
        batch.bind()

    assert "alpha:1:" in str(error.value)
    assert message in str(error.value)


def test_cross_formula_reference_is_rejected() -> None:
    """验证拒绝跨公式引用其他公式的局部绑定。"""
    batch = FormulaBatch.from_text(
        formulas={
            "one": "private = 1",
            "two": "factor = private + 1",
        }
    )

    with pytest.raises(SymbolBindingError, match="cross-formula reference 'private'"):
        batch.bind()


def test_common_input_cannot_be_shadowed() -> None:
    """验证公共输入不能被公式内的局部绑定遮蔽。"""
    batch = FormulaBatch.from_text(
        common_inputs="close = source('stk.1d.close')",
        formulas={"alpha": "close = 1"},
    )

    with pytest.raises(SymbolBindingError, match="shadows a common input"):
        batch.bind()


def test_ast_source_ref_is_immutable_and_semantic() -> None:
    """验证数据源引用表达式不可变且按语义比较。"""
    ref = SourceRefExpr.create("stk.1d.close", adjusted=True)

    assert ref.logical_key == "stk.1d.close"
    assert ref.params["adjusted"] is True
    with pytest.raises(TypeError):
        ref.params["adjusted"] = False


def test_python_helpers_build_ast_without_reading_or_global_registration() -> None:
    """验证 Python helper 只构建表达式而不读取数据或全局注册。"""
    close = get_lf("stk", "close", policy={"adjusted": True})
    expression = operator("ts_mean", close, 5)

    assert isinstance(close, SourceRefExpr)
    assert close.semantic_params == (("policy", (("adjusted", True),)),)
    assert isinstance(expression, OperatorExpr)
    assert expression.args[0] is close


def test_resample_is_a_public_operator_and_get_hf_is_equivalent_sugar() -> None:
    """验证 resample 是公开算子且 get_hf 语法糖与其等价。"""
    raw = get_hf("stk", "1min", "ClosePrice", adjusted=True)
    direct = operator(
        "resample", raw, target_freq="15min", method="last"
    )
    sugar = get_hf(
        "stk",
        "1min",
        "ClosePrice",
        adjusted=True,
        resample="15min",
        method="last",
    )
    parsed = FormulaParser().parse_program(
        "factor = resample(x, '15min', method='last')", source="alpha"
    )

    assert sugar == direct
    assert "resample" in default_operator_registry()
    assert isinstance(parsed.bindings[0].expression, OperatorExpr)
    assert parsed.bindings[0].expression.name == "resample"

    aligned = (
        FormulaParser()
        .parse_program(
            "factor = align_frequency(x, '1min', method='ffill')", source="alpha"
        )
        .bindings[0]
        .expression
    )
    assert isinstance(aligned, OperatorExpr)
    assert aligned.name == "align_frequency"
    assert "align_frequency" in default_operator_registry()


def test_get_hf_rejects_resample_configuration_without_its_pair() -> None:
    """验证 get_hf 拒绝单独提供 resample 或 method 的配置。"""
    with pytest.raises(ValueError, match="resample requires an explicit method"):
        get_hf("stk", "1min", "ClosePrice", resample="15min")
    with pytest.raises(ValueError, match="method requires resample"):
        get_hf("stk", "1min", "ClosePrice", method="last")
