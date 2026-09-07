"""旧版 FeatureStore 测试的公共 fixture（沿用旧版“特征”术语）。"""

from __future__ import annotations

import pandas as pd
import pytest

from factor_engine.legacy import FeatureStore


class CalendarReader:
    """提供最小 SQL 边界，用于构造确定性的测试快照。"""

    def _read_sql(self, sql: str) -> pd.DataFrame:
        """返回固定的交易日历，仅接受日历表查询。"""
        if "CalenderDay_TradingDay" not in sql:
            raise AssertionError(f"Unexpected SQL in test fixture: {sql}")
        return pd.DataFrame({"DataDate": ["2024-01-02", "2024-01-03", "2024-01-04"]})


@pytest.fixture
def store(tmp_path) -> FeatureStore:
    """构造并初始化一个含固定日历与资产的特征存储快照。"""
    store = FeatureStore(tmp_path / "snapshot")
    store.init_snapshot(
        start="2024-01-01",
        end="2024-01-31",
        assets=("stk",),
        asset_codes={"stk": [101, 202]},
        reader=CalendarReader(),
    )
    return store
