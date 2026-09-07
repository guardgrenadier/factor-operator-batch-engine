"""覆盖 sql_reader 与具名 Query Builder（panel_fields/adjust_factor/untradable）的测试。"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from factor_engine import (
    BatchFactorEngine,
    ComputeRequest,
    DomainSpec,
    FormulaBatch,
    SmartQuantDataProvider,
)


class _SqlReader:
    """按查询特征返回各类 SQL 源预制表数据的模拟后端。"""

    def __init__(self) -> None:
        """初始化 SQL 记录列表。"""
        self.sql: list[str] = []

    def query(self, sql: str) -> pd.DataFrame:
        """记录 SQL 并按物理表返回对应的模拟 DataFrame。"""
        self.sql.append(sql)
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["ItemCode", "ItemName"])
        if "JY_TradingDayNew" in sql:
            return pd.DataFrame({"TradingDate": ["20240102", "20240103"]})
        if "SELECT DISTINCT" in sql:
            return pd.DataFrame({"InnerCode": [101, 202]})
        if "DZ_AdjustingFactor" in sql and "RatioAdjustingFactor" in sql:
            return pd.DataFrame(
                {
                    "DataDate": ["20240102", "20240102", "20240103", "20240103"],
                    "InnerCode": [101, 202, 101, 202],
                    "value_0": [1.0, 2.0, 1.5, 2.0],
                }
            )
        if "Untradable" in sql:
            return pd.DataFrame(
                {
                    "DataDate": ["20240102"],
                    "InnerCode": [101],
                    "value_0": [1.0],
                }
            )
        if "IndexComponentWeight_Choice" in sql:
            rows = pd.DataFrame(
                {
                    "DataDate": ["20240102", "20240103"],
                    "InnerCode": [101, 202],
                    "value_0": [0.25, 0.75],
                }
            )
            # 成员绑定由 SQL 常量投影；模拟后端按别名补常量列。
            for alias in re.findall(r"1\.0 AS `?(value_\d+)`?", sql):
                rows[alias] = 1.0
            return rows
        raise AssertionError(sql)


def _provider() -> SmartQuantDataProvider:
    """构造含复权、不可交易与指数成分源的测试数据提供方。"""
    return SmartQuantDataProvider(
        backend=_SqlReader(),
        source_config={
            "schema_version": 3,
            "source_tables": [
                {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "ReturnDaily",
                    "table": "SmartQuant.ReturnDaily",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "asset_axis": True,
                    "date_col": "DataDate",
                    "trading_flag_col": "IfTradingDay",
                    "fields": ["DataDate", "InnerCode", "IfTradingDay"],
                }
            ],
            "sources": {
                "stk.1d.adj_factor": {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "AdjustFactor",
                    "table": "JYDB.DZ_AdjustingFactor",
                    "reader": "sql_reader",
                    "query_builder": "adjust_factor",
                    "field": "adj_factor",
                },
                "stk.1d.is_untradable": {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "Untradable",
                    "table": "SmartQuant.Untradable",
                    "reader": "sql_reader",
                    "query_builder": "untradable",
                    "field": "is_untradable",
                    "value_kind": "mask",
                    "params": {"default": 0.0},
                },
                "stk.1d.index_weight.CSI300": {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "IndexComponentWeight_Choice",
                    "table": "SmartQuant.IndexComponentWeight_Choice",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "date_col": "EndDate",
                    "code_col": "SecuInnerCode",
                    "field": "Weight",
                    "params": {"index_inner_code": 3145},
                },
                "stk.1d.is_member.CSI300": {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "IndexComponentWeight_Choice",
                    "table": "SmartQuant.IndexComponentWeight_Choice",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "date_col": "EndDate",
                    "code_col": "SecuInnerCode",
                    "field": "Weight",
                    "value_kind": "mask",
                    "params": {
                        "index_inner_code": 3145,
                        "constant": 1,
                        "default": 0.0,
                    },
                },
            },
        },
    )


def test_adjust_factor_builder_generates_as_of_sql() -> None:
    """验证 adjust_factor 的 as-of 相关查询 SQL 与数值加载。"""
    provider = _provider()
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="adj = source('stk.1d.adj_factor')",
            formulas={"alpha": "factor = adj"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    sql = next(sql for sql in provider.backend.sql if "DZ_AdjustingFactor" in sql)
    assert "ORDER BY a.ExDiviDate DESC LIMIT 1" in sql
    assert "COALESCE((" in sql
    np.testing.assert_allclose(
        result.arrays["alpha"][:, :, 0],
        [[1.0, 2.0], [1.5, 2.0]],
    )


def test_untradable_builder_derives_mask_and_default_fills_missing() -> None:
    """验证 untradable 派生 mask 字段，缺失行由显式默认值填 0。"""
    provider = _provider()
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="untradable = source('stk.1d.is_untradable')",
            formulas={"alpha": "factor = untradable"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    sql = next(sql for sql in provider.backend.sql if "Untradable" in sql)
    assert "CASE WHEN" in sql and "COALESCE(" in sql
    np.testing.assert_array_equal(
        result.arrays["alpha"][:, :, 0],
        [[1.0, 0.0], [0.0, 0.0]],
    )


def test_panel_fields_share_one_sql_for_weight_and_membership() -> None:
    """验证指数权重与成员常量投影共享一次 panel_fields 查询。"""
    provider = _provider()
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "weight = source('stk.1d.index_weight.CSI300')\n"
                "member = source('stk.1d.is_member.CSI300')"
            ),
            formulas={
                "weight": "factor = weight",
                "member": "factor = member",
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    sql = next(
        sql for sql in provider.backend.sql if "IndexComponentWeight_Choice" in sql
    )
    assert "IndexInnerCode = 3145" in sql
    assert "`EndDate` AS DataDate" in sql
    assert "`SecuInnerCode` AS InnerCode" in sql
    assert "1.0 AS `value_1`" in sql
    assert result.stats.load_calls == 1
    np.testing.assert_allclose(
        result.arrays["weight"][:, :, 0],
        [[0.25, np.nan], [np.nan, 0.75]],
        equal_nan=True,
    )
    np.testing.assert_array_equal(
        result.arrays["member"][:, :, 0],
        [[1.0, 0.0], [0.0, 1.0]],
    )
