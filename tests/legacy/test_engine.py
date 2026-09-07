"""旧版特征解析器与计算器的测试（沿用旧版“特征”术语）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.legacy import Calculator, DataRouter
from factor_engine.legacy.engine import FeatureExpr, FormulaParser, OpExpr


def test_parser_rewrites_dotted_feature_keys() -> None:
    """验证旧版解析器将带点特征键改写为特征引用节点。"""
    expr = FormulaParser().parse(
        "divide(stk.1d.ClosePrice, delay(stk.1d.PrevClosePrice, periods=1))"
    )

    assert isinstance(expr, OpExpr)
    assert expr.op == "divide"
    assert isinstance(expr.args[0], FeatureExpr)
    assert expr.args[0].key == "stk.1d.ClosePrice"
    assert isinstance(expr.args[1], OpExpr)
    assert expr.args[1].op == "delay"
    assert isinstance(expr.args[1].args[0], FeatureExpr)
    assert expr.args[1].args[0].key == "stk.1d.PrevClosePrice"
    assert expr.args[1].kwargs == {"periods": 1}


class EmptySourceDirectoryReader:
    """返回空源目录并拒绝其他 SQL 的读取器。"""

    def _read_sql(self, sql: str) -> pd.DataFrame:
        """仅接受源目录查询并返回空结果。"""
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["field", "name_cn"])
        raise AssertionError(f"Unexpected SQL in test: {sql}")


def test_calculator_returns_array_result_without_materializing(store) -> None:
    """验证计算器返回内存数组结果且不落盘物化。"""
    router = DataRouter(
        source_config={"source_tables": [], "sources": {}},
        reader=EmptySourceDirectoryReader(),
        memory_data={"stk.1d.raw": np.ones((3, 2))},
    )
    calculator = Calculator(store, data_router=router)

    result = calculator.calculate("add(stk.1d.raw, 1)", output="stk.1d.runtime")

    np.testing.assert_allclose(result.values, np.full((3, 2, 1), 2.0))
    assert result.key == "stk.1d.runtime"
    assert not store.has_feature("stk.1d.runtime")
