"""覆盖 SmartQuant 数据提供方的加载组、资产轴与分钟分区读取行为的测试。"""

from __future__ import annotations

import re
import sys

import numpy as np
import pandas as pd
import pytest

from factor_engine import (
    BatchFactorEngine,
    ComputeRequest,
    DataProviderError,
    DomainSpec,
    ExecutionOptions,
    FormulaBatch,
    OperatorTerm,
    SmartQuantDataProvider,
    SourceRefExpr,
    SourceTerm,
    ValueKind,
)
from factor_engine.data_provider.backend import DuckDBBackend
from factor_engine.data_provider.catalog import load_config, validate_config
from factor_engine.domain import get_freq_step_count, get_freq_step_values


AXIS_SOURCE = {
    "asset": "stk",
    "freq": "1d",
    "source": "ReturnDaily",
    "table": "SmartQuant.ReturnDaily",
    "reader": "sql_reader",
    "query_builder": "panel_fields",
    "asset_axis": True,
    "date_col": "DataDate",
    "trading_flag_col": "IfTradingDay",
    "fields": ["DataDate", "InnerCode", "SecuCode", "IfTradingDay"],
}


class _Reader:
    """记录 SQL 并按查询特征返回固定表数据的模拟后端。"""

    def __init__(self) -> None:
        """初始化 SQL 记录列表。"""
        self.sql: list[str] = []

    def query(self, sql: str) -> pd.DataFrame:
        """记录 SQL 并按查询内容返回对应的模拟 DataFrame。"""
        self.sql.append(sql)
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["ItemCode", "ItemName"])
        if "JY_TradingDayNew" in sql:
            return pd.DataFrame({"TradingDate": ["20240102", "20240103", "20240104"]})
        if "SELECT DISTINCT" in sql:
            return pd.DataFrame({"InnerCode": [101, 202]})
        if "InnerCode_SecuCode" in sql:
            return pd.DataFrame(
                {"InnerCode": [101], "SecuCode": ["000001"]}
            )
        if "SecuCode" in sql:
            return pd.DataFrame(
                {
                    "DataDate": ["20240102"],
                    "InnerCode": [101],
                    "SecuCode": ["000001"],
                }
            )
        if "FROM `SmartQuant`.`ReturnDaily`" in sql:
            rows = pd.DataFrame(
                {
                    "DataDate": [
                        "20240102",
                        "20240102",
                        "20240103",
                        "20240103",
                        "20240104",
                        "20240104",
                    ],
                    "InnerCode": [101, 202, 101, 202, 101, 202],
                }
            )
            aliases = re.findall(r"AS `?(value_\d+)`?", sql)
            for alias in aliases:
                rows[alias] = range(1, 7) if alias == "value_0" else 10.0
            return rows
        raise AssertionError(sql)


class _CodeColReader(_Reader):
    """支持自定义资产编码列的返回日数据模拟后端。"""

    def query(self, sql: str) -> pd.DataFrame:
        """对明细查询记录 SQL 并返回带自定义编码列的数据。"""
        if "FROM `SmartQuant`.`ReturnDaily`" in sql and "SELECT DISTINCT" not in sql:
            self.sql.append(sql)
            rows = pd.DataFrame(
                {
                    "DataDate": [
                        "20240102",
                        "20240102",
                        "20240103",
                        "20240103",
                    ],
                    "InnerCode": [101, 202, 101, 202],
                }
            )
            aliases = re.findall(r"AS `?(value_\d+)`?", sql)
            for alias in aliases:
                rows[alias] = [1.0, 2.0, 3.0, 4.0]
            return rows
        return super().query(sql)


class _ProjectionReader:
    """模拟股票与可转债资产轴及股债映射表的查询后端。"""

    def __init__(self) -> None:
        """初始化 SQL 记录列表。"""
        self.sql: list[str] = []

    def query(self, sql: str) -> pd.DataFrame:
        """记录 SQL 并按股票/可转债/映射表查询返回模拟数据。"""
        self.sql.append(sql)
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["ItemCode", "ItemName"])
        if "JY_TradingDayNew" in sql:
            return pd.DataFrame({"TradingDate": ["20240102", "20240103"]})
        if "SELECT DISTINCT" in sql and "CBReturnDaily" in sql:
            return pd.DataFrame({"InnerCode": [101, 102, 103]})
        if "SELECT DISTINCT" in sql and "ReturnDaily" in sql:
            return pd.DataFrame({"InnerCode": [11, 33]})
        if "Bond_ConBDBasicInfo" in sql:
            return pd.DataFrame({"InnerCode": [101, 102, 103], "value": [11, 33, 22]})
        if "FROM `SmartQuant`.`ReturnDaily`" in sql:
            rows = pd.DataFrame(
                {
                    "DataDate": [
                        "20240102",
                        "20240102",
                        "20240103",
                        "20240103",
                    ],
                    "InnerCode": [33, 11, 33, 11],
                }
            )
            alias = re.search(r"AS `?(value_\d+)`?", sql)
            assert alias is not None
            rows[alias.group(1)] = [30.0, 10.0, 31.0, 11.0]
            if "BETWEEN '20240102' AND '20240102'" in sql:
                rows = rows[rows["DataDate"] == "20240102"]
            elif "BETWEEN '20240103' AND '20240103'" in sql:
                rows = rows[rows["DataDate"] == "20240103"]
            return rows
        raise AssertionError(sql)


class _MinuteReader:
    """基于资产编码映射返回日历与编码数据的分钟级模拟后端。"""

    def __init__(self, code_map: pd.DataFrame) -> None:
        """保存编码映射表并初始化 SQL 记录列表。"""
        self.code_map = code_map
        self.sql: list[str] = []

    def query(self, sql: str) -> pd.DataFrame:
        """记录 SQL 并按日历、编码或映射查询返回模拟数据。"""
        self.sql.append(sql)
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["ItemCode", "ItemName"])
        if "JY_TradingDayNew" in sql:
            return pd.DataFrame(
                {"TradingDate": sorted(self.code_map["DataDate"].unique())}
            )
        if "SELECT DISTINCT" in sql:
            return pd.DataFrame(
                {"InnerCode": sorted(self.code_map["InnerCode"].unique())}
            )
        if "InnerCode_SecuCode" in sql:
            return self.code_map[["InnerCode", "SecuCode"]].drop_duplicates()
        if "SecuCode" in sql:
            return self.code_map.copy()
        raise AssertionError(sql)


def _minute_source(template: str, fields: list[str]) -> dict[str, object]:
    """构造分钟级 parquet 分区的物理源规格配置。"""
    return {
        "asset": "stk",
        "freq": "1min",
        "source": "MinuteParquet",
        "table": template,
        "reader": "parquet_bars",
        "path_template": template,
        "date_col": "trading_day",
        "date_col_type": "date",
        "code_col": "security_code",
        "code_identity": "secu_code",
        "fields": [
            "trading_day",
            "security_code",
            "start_time",
            *fields,
        ],
    }


def _legacy_minute_arrays(
    paths, code_map: pd.DataFrame, dates, codes, steps
) -> dict[str, np.ndarray]:
    """用逐行字典散点方式为分钟数据源构造基准值数组。"""
    shape = (len(dates), len(codes), len(steps))
    result = {
        name: np.full(shape, np.nan, dtype=np.float64) for name in ("close", "volume")
    }
    step_pos = {step: pos for pos, step in enumerate(steps)}
    code_pos = {code: pos for pos, code in enumerate(codes)}
    mapping = {
        date: dict(zip(group["SecuCode"], group["InnerCode"], strict=True))
        for date, group in code_map.groupby("DataDate")
    }
    for date_idx, (date, path) in enumerate(zip(dates, paths, strict=True)):
        frame = pd.read_parquet(path)
        for row in frame.itertuples(index=False):
            inner = mapping[date].get(str(row.security_code))
            if inner not in code_pos or row.start_time not in step_pos:
                continue
            for name in result:
                value = float(getattr(row, name))
                result[name][date_idx, code_pos[inner], step_pos[row.start_time]] = (
                    value if np.isfinite(value) else np.nan
                )
    return result


def test_catalog_config_validation_rejects_silent_misbindings() -> None:
    """验证 Catalog 在外部 I/O 前拒绝逻辑键、代码身份和数据集冲突。"""
    source = {
        "asset": "stk",
        "freq": "1d",
        "source": "Test",
        "table": "Schema.Table",
        "reader": "sql_reader",
        "query_builder": "panel_fields",
        "field": "value",
    }
    cases = [
        (
            {
                "schema_version": 3,
                "source_tables": [],
                "sources": {"stk.1d.value": {**source, "asset": "cb"}},
            },
            "asset/freq must match",
        ),
        (
            {
                "schema_version": 3,
                "source_tables": [
                    {**source, "fields": ["value"], "code_identity": "secu_cdoe"}
                ],
                "sources": {},
            },
            "invalid code_identity",
        ),
        (
            {
                "schema_version": 3,
                "source_tables": [],
                "sources": {
                    "stk.1d.a": {
                        **source,
                        "dataset_id": "same",
                        "field": "a",
                    },
                    "stk.1d.b": {
                        **source,
                        "dataset_id": "same",
                        "table": "Schema.Other",
                        "field": "b",
                    },
                },
            },
            "conflicts with dataset_id",
        ),
    ]

    validate_config(load_config(None))
    for config, message in cases:
        with pytest.raises(DataProviderError, match=message):
            validate_config(config)

    shared = {
        "schema_version": 3,
        "source_tables": [
            {
                **source,
                "fields": ["DataDate", "InnerCode"],
                "trading_flag_col": "IfTradingDay",
            }
        ],
        "sources": {"stk.1d.value": source},
    }
    provider = SmartQuantDataProvider(backend=_Reader(), source_config=shared)
    spec, _ = provider.catalog.bind(SourceRefExpr.create("stk.1d.value"))
    assert spec.params["trading_flag_col"] == "IfTradingDay"


def test_formula_cannot_override_catalog_physical_parameters() -> None:
    """验证 Source semantic params 不能篡改 Catalog 的物理坐标配置。"""
    key = "stk.1d.value"
    provider = SmartQuantDataProvider(
        backend=_Reader(),
        source_config={
            "schema_version": 3,
            "source_tables": [],
            "sources": {
                key: {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "Test",
                    "table": "Schema.Table",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "field": "value",
                }
            },
        },
        include_tables=[key],
    )

    with pytest.raises(DataProviderError, match="cannot override physical"):
        provider.describe_many([SourceRefExpr.create(key, date_col="WrongDate")])


def _projection_provider() -> SmartQuantDataProvider:
    """构造含股票、可转债与股债映射源的投影数据提供方。"""
    return SmartQuantDataProvider(
        backend=_ProjectionReader(),
        source_config={
            "schema_version": 3,
            "source_tables": [
                {
                    **AXIS_SOURCE,
                    "fields": [
                        "DataDate",
                        "InnerCode",
                        "SecuCode",
                        "IfTradingDay",
                        "ClosePrice",
                    ],
                },
                {
                    "asset": "cb",
                    "freq": "1d",
                    "source": "CBReturnDaily",
                    "table": "SmartQuant.CBReturnDaily",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "asset_axis": True,
                    "date_col": "DataDate",
                    "trading_flag_col": "IfTradingDay",
                    "fields": ["DataDate", "InnerCode", "IfTradingDay"],
                },
            ],
            "sources": {
                "cb.1d.underlying_stk": {
                    "asset": "cb",
                    "freq": "1d",
                    "source": "CBStockMap",
                    "table": "JYDB.Bond_ConBDBasicInfo",
                    "reader": "cb_stock_map",
                    "field": "StockInnerCode",
                    "value_kind": "code",
                    "params": {"projection": "axis_position"},
                }
            },
        },
    )


def test_task_domain_lookback_and_daily_fields_use_one_sql() -> None:
    """验证含历史回看的日频字段用单条批量 SQL 加载。"""
    reader = _Reader()
    provider = SmartQuantDataProvider(
        backend=reader,
        source_config={
            "schema_version": 3,
            "source_tables": [
                {
                    **AXIS_SOURCE,
                    "fields": [
                        "DataDate",
                        "InnerCode",
                        "IfTradingDay",
                        "ClosePrice",
                        "TurnoverVolume",
                        "IndustryCodeNew",
                        "IndustryNameNew",
                    ],
                }
            ],
            "sources": {},
        },
    )
    batch = FormulaBatch.from_text(
        common_inputs="close=get_lf('stk','ClosePrice')\nvolume=get_lf('stk','TurnoverVolume')",
        formulas={"alpha": "factor=ts_mean(close, 2) + volume"},
    )
    result = BatchFactorEngine(provider).compute(
        ComputeRequest(
            DomainSpec("20240103", "20240104", {"stk": "all"}, "stk", "1d", 1),
            batch,
        )
    )

    load_events = [
        event for event in provider.diagnostics if event["operation"] == "load"
    ]
    assert result.arrays["alpha"].shape == (2, 2, 1)
    assert result.stats.load_calls == 1
    assert len(load_events) == 1
    assert load_events[0]["fields"] == ["ClosePrice", "TurnoverVolume"]
    assert load_events[0]["mode"] == "batch"
    industry = SourceRefExpr.create("stk.1d.IndustryCodeNew")
    assert provider.describe_many([industry])[industry].value_kind is ValueKind.CODE
    excluded = SourceRefExpr.create("stk.1d.IndustryNameNew")
    with pytest.raises(DataProviderError, match="Unknown source"):
        provider.describe_many([excluded])
    axis_sql = next(sql for sql in reader.sql if "SELECT DISTINCT" in sql)
    assert "BETWEEN '20240102' AND '20240104'" in axis_sql


def test_explicit_axis_is_validated_and_keeps_caller_order() -> None:
    """验证显式资产轴经校验并保留调用方给定顺序。"""
    reader = _Reader()
    provider = SmartQuantDataProvider(
        backend=reader,
        source_config={
            "schema_version": 3,
            "source_tables": [AXIS_SOURCE],
            "sources": {},
        },
    )

    codes = provider.asset_codes("stk", ["20240103", "20240104"], [202, 101])

    assert codes.tolist() == [202, 101]


def test_wide_table_supports_custom_code_col() -> None:
    """验证特征宽表支持自定义资产编码列（资产轴则固定用注册表参数）。"""
    reader = _CodeColReader()
    provider = SmartQuantDataProvider(
        backend=reader,
        source_config={
            "schema_version": 3,
            "source_tables": [
                {
                    **AXIS_SOURCE,
                    "code_col": "SecurityCode",
                    "fields": [
                        "DataDate",
                        "InnerCode",
                        "SecurityCode",
                        "IfTradingDay",
                        "ClosePrice",
                    ],
                }
            ],
            "sources": {},
        },
    )
    batch = FormulaBatch.from_text(
        common_inputs="close=get_lf('stk','ClosePrice')",
        formulas={"alpha": "factor=close"},
    )
    result = BatchFactorEngine(provider).compute(
        ComputeRequest(
            DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
            batch,
        )
    )

    # 资产轴来自代码注册表，固定使用 InnerCode，不随数据集配置变化。
    axis_sql = next(sql for sql in reader.sql if "SELECT DISTINCT" in sql)
    assert "`InnerCode` AS InnerCode" in axis_sql
    wide_sql = next(
        sql
        for sql in reader.sql
        if "FROM `SmartQuant`.`ReturnDaily`" in sql and "SELECT DISTINCT" not in sql
    )
    assert "`SecurityCode` AS InnerCode" in wide_sql
    assert "WHERE `DataDate` BETWEEN" in wide_sql
    assert "`SecurityCode` IN (101, 202)" in wide_sql
    assert result.arrays["alpha"].shape == (2, 2, 1)
    np.testing.assert_allclose(
        result.arrays["alpha"][:, :, 0],
        [[1.0, 2.0], [3.0, 4.0]],
    )


def test_minute_fields_share_one_parquet_scan(tmp_path, monkeypatch) -> None:
    """验证多个分钟字段在同一次 parquet 扫描中加载。"""

    # 用可捕获 SQL 与轴表的 DuckDB 后端记录分钟扫描细节。
    class CapturingDuckDB(DuckDBBackend):
        """捕获分钟扫描 SQL、映射轴表与扫描次数的后端。"""

        def __init__(self):
            """初始化捕获字段与扫描计数。"""
            super().__init__()
            self.code_map_columns = None
            self.file_axis = None
            self.asset_axis = None
            self.minute_sql = None
            self.scan_count = 0

        def iter_arrow(self, sql, *, tables=None, threads=None, batch_rows=None):
            """记录扫描次数与轴表后委托父类迭代结果。"""
            self.scan_count += 1
            self.minute_sql = sql
            self.code_map_columns = tables["code_map"].columns.tolist()
            self.file_axis = tables["file_axis"].to_dict("list")
            self.asset_axis = tables["asset_axis"].to_dict("list")
            yield from super().iter_arrow(
                sql,
                tables=tables,
                threads=threads,
                batch_rows=batch_rows,
            )

    pd.DataFrame(
        {
            "trading_day": pd.to_datetime(["2024-01-02", "2024-01-02"]),
            "security_code": ["000001", "000001"],
            "start_time": [931, 932],
            "close": [1.0, 2.0],
            "volume": [10.0, 20.0],
        }
    ).to_parquet(tmp_path / "20240102.parquet")
    path_type = type(tmp_path)
    original_exists = path_type.exists

    def reject_preflight_stat(path):
        """断言正常分钟加载不预先探测分区文件是否存在。"""
        if path == tmp_path / "20240102.parquet":
            raise AssertionError(f"normal minute load must not stat {path}")
        return original_exists(path)

    monkeypatch.setattr(path_type, "exists", reject_preflight_stat)
    original_copy = pd.DataFrame.copy

    def reject_dataset_copy(frame, *args, **kwargs):
        """断言查询数据集内部 DataFrame 不被额外复制。"""
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller in {
            "factor_engine.data_provider.readers",
            "factor_engine.data_provider.normalize",
        }:
            raise AssertionError("private query DataFrame must not be copied")
        return original_copy(frame, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "copy", reject_dataset_copy)
    template = str(tmp_path / "{date}.parquet")
    duckdb = CapturingDuckDB()
    reader = _Reader()
    provider = SmartQuantDataProvider(
        backend=reader,
        duckdb=duckdb,
        source_config={
            "schema_version": 3,
            "source_tables": [
                AXIS_SOURCE,
                {
                    **_minute_source(template, ["close", "volume"]),
                },
            ],
                        "sources": {},
        },
    )
    batch = FormulaBatch.from_text(
        common_inputs="close=get_hf('stk','1min','close')\nvolume=get_hf('stk','1min','volume')",
        formulas={"alpha": "factor=close + volume"},
    )

    result = BatchFactorEngine(provider).compute(
        ComputeRequest(
            DomainSpec(
                "20240102",
                "20240102",
                {"stk": [101]},
                "stk",
                "1min",
                get_freq_step_count("1min"),
            ),
            batch,
        )
    )

    scan = [event for event in provider.diagnostics if event["operation"] == "load"]
    assert scan[0]["fields"] == ["close", "volume"]
    assert result.arrays["alpha"][0, 0, 1:3].tolist() == [11.0, 22.0]
    assert duckdb.scan_count == 1
    assert duckdb.minute_sql.count("read_parquet(") == 1
    assert "filename=true" in duckdb.minute_sql
    assert "trading_day" not in duckdb.minute_sql
    assert "CAST(p.security_code" not in duckdb.minute_sql
    assert 'cast_to_type(m.SecuCode, p."security_code")' in duckdb.minute_sql
    assert "AS DOUBLE" in duckdb.minute_sql
    assert "AS flat_idx" in duckdb.minute_sql
    assert "start_time IN" not in duckdb.minute_sql
    assert duckdb.code_map_columns == ["InnerCode", "SecuCode"]
    assert duckdb.file_axis == {
        "filename": [str(tmp_path / "20240102.parquet")],
        "date_key": ["20240102"],
        "date_idx": [0],
    }
    assert duckdb.asset_axis == {"InnerCode": [101], "asset_idx": [0]}
    mapping_sql = next(sql for sql in reader.sql if "InnerCode_SecuCode" in sql)
    assert "WHERE InnerCode IN (101)" in mapping_sql
    assert "DataDate" not in mapping_sql


def test_cb_minute_keeps_date_dependent_code_map(tmp_path) -> None:
    """验证可转债分钟读取保留随日期变化的编码映射。"""
    pd.DataFrame(
        {
            "trading_day": pd.to_datetime(["2024-01-02"]),
            "security_code": ["110001"],
            "start_time": [930],
            "close": [1.5],
        }
    ).to_parquet(tmp_path / "20240102.parquet")
    reader = _MinuteReader(
        pd.DataFrame(
            {
                "DataDate": ["20240102"],
                "InnerCode": [501],
                "SecuCode": ["110001"],
            }
        )
    )
    template = str(tmp_path / "{date}.parquet")
    provider = SmartQuantDataProvider(
        backend=reader,
        source_config={
            "schema_version": 3,
            "source_tables": [
                {
                    "asset": "cb",
                    "freq": "1d",
                    "source": "CBReturnDaily",
                    "table": "SmartQuant.CBReturnDaily",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "asset_axis": True,
                    "date_col": "DataDate",
                    "trading_flag_col": "IfTradingDay",
                    "fields": ["DataDate", "InnerCode", "SecuCode", "IfTradingDay"],
                },
                {
                    **_minute_source(template, ["close"]),
                    "asset": "cb",
                },
            ],
                        "sources": {},
        },
    )
    result = BatchFactorEngine(provider).compute(
        ComputeRequest(
            DomainSpec(
                "20240102",
                "20240102",
                {"cb": [501]},
                "cb",
                "1min",
                get_freq_step_count("1min"),
            ),
            FormulaBatch.from_text(
                common_inputs="close=get_hf('cb','1min','close')",
                formulas={"close": "factor=close"},
            ),
        )
    )

    assert result.arrays["close"][0, 0, 0] == 1.5
    assert not any("InnerCode_SecuCode" in sql for sql in reader.sql)
    assert any("FROM `SmartQuant`.`CBReturnDaily`" in sql for sql in reader.sql)


def test_minute_streaming_matches_shape_dtype_values_and_alignment(tmp_path) -> None:
    """验证分钟流式读取的形状、类型、数值与逐行散点基线一致。"""
    # 构造编码映射与含缺失、无穷的乱序分钟原始行。
    code_map = pd.DataFrame(
        {
            "DataDate": ["20240102", "20240102", "20240103", "20240103"],
            "InnerCode": [101, 202, 101, 202],
            "SecuCode": ["000001", "000002", "000001", "000002"],
        }
    )
    rows = {
        "20240102": pd.DataFrame(
            {
                "trading_day": pd.to_datetime(["2024-01-02"] * 4),
                "security_code": ["000002", "000001", "000002", "000001"],
                "start_time": [932, 930, 930, 932],
                "close": [22.0, 10.0, np.inf, np.nan],
                "volume": [220.0, 100.0, 200.0, 120.0],
            }
        ),
        "20240103": pd.DataFrame(
            {
                "trading_day": pd.to_datetime(["2024-01-03"] * 4),
                "security_code": ["000001", "000002", "000001", "000002"],
                "start_time": [931, 930, 932, 932],
                "close": [31.0, 40.0, 32.0, 42.0],
                "volume": [310.0, 400.0, 320.0, 420.0],
            }
        ),
    }
    for date, frame in rows.items():
        frame.to_parquet(tmp_path / f"{date}.parquet")
    template = str(tmp_path / "{date}.parquet")
    provider = SmartQuantDataProvider(
        backend=_MinuteReader(code_map),
        duckdb=DuckDBBackend(arrow_batch_rows=2),
        source_config={
            "schema_version": 3,
            "source_tables": [
                AXIS_SOURCE,
                _minute_source(template, ["close", "volume"]),
            ],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": [202, 101]},
            "stk",
            "1min",
            get_freq_step_count("1min"),
        ),
        FormulaBatch.from_text(
            common_inputs=(
                "close=get_hf('stk','1min','close')\n"
                "volume=get_hf('stk','1min','volume')"
            ),
            formulas={"close": "factor=close", "volume": "factor=volume"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    # 用逐行散点基线重建基准值数组。
    legacy = _legacy_minute_arrays(
        [tmp_path / "20240102.parquet", tmp_path / "20240103.parquet"],
        code_map,
        ("20240102", "20240103"),
        (202, 101),
        tuple(get_freq_step_values("1min")),
    )

    assert result.arrays["close"].shape == (2, 2, 237)
    assert result.arrays["close"].dtype == np.float64
    assert result.arrays["volume"].dtype == np.float64
    np.testing.assert_allclose(
        result.arrays["close"][:, :, :3],
        np.array(
            [
                [[np.nan, np.nan, 22.0], [10.0, np.nan, np.nan]],
                [[40.0, np.nan, 42.0], [np.nan, 31.0, 32.0]],
            ]
        ),
        equal_nan=True,
    )
    np.testing.assert_allclose(
        result.arrays["volume"][:, :, :3],
        np.array(
            [
                [[200.0, np.nan, 220.0], [100.0, np.nan, 120.0]],
                [[400.0, np.nan, 420.0], [np.nan, 310.0, 320.0]],
            ]
        ),
        equal_nan=True,
    )
    assert np.isnan(result.arrays["close"][0, 0, 0])
    assert np.isnan(result.arrays["close"][0, 1, 2])
    np.testing.assert_allclose(result.arrays["close"], legacy["close"], equal_nan=True)
    np.testing.assert_allclose(
        result.arrays["volume"], legacy["volume"], equal_nan=True
    )


@pytest.mark.parametrize(
    ("batch_rows", "case"), [(2, "same batch"), (1, "across batches")]
)
def test_minute_streaming_rejects_duplicate_coordinates(
    tmp_path, batch_rows, case
) -> None:
    """验证分钟流式读取拒绝重复的日期/资产/step 坐标。"""
    del case
    pd.DataFrame(
        {
            "trading_day": pd.to_datetime(["2024-01-02", "2024-01-02"]),
            "security_code": ["000001", "000001"],
            "start_time": [930, 930],
            "close": [1.0, 2.0],
        }
    ).to_parquet(tmp_path / "20240102.parquet")
    code_map = pd.DataFrame(
        {
            "DataDate": ["20240102"],
            "InnerCode": [101],
            "SecuCode": ["000001"],
        }
    )
    template = str(tmp_path / "{date}.parquet")
    provider = SmartQuantDataProvider(
        backend=_MinuteReader(code_map),
        duckdb=DuckDBBackend(arrow_batch_rows=batch_rows),
        source_config={
            "schema_version": 3,
            "source_tables": [
                AXIS_SOURCE,
                _minute_source(template, ["close"]),
            ],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240102",
            {"stk": [101]},
            "stk",
            "1min",
            get_freq_step_count("1min"),
        ),
        FormulaBatch.from_text(
            common_inputs="close=get_hf('stk','1min','close')",
            formulas={"close": "factor=close"},
        ),
    )

    with pytest.raises(
        DataProviderError,
        match="Backend returned duplicate date/asset/step coordinates",
    ):
        BatchFactorEngine(provider).compute(request)


def test_minute_group_fails_when_any_partition_file_is_missing(tmp_path) -> None:
    """验证任一分区文件缺失时加载组整体报错。"""
    pd.DataFrame(
        {
            "trading_day": pd.to_datetime(["2024-01-02"]),
            "security_code": ["000001"],
            "start_time": [931],
            "close": [1.0],
        }
    ).to_parquet(tmp_path / "20240102.parquet")
    template = str(tmp_path / "{date}.parquet")
    provider = SmartQuantDataProvider(
        backend=_Reader(),
        source_config={
            "schema_version": 3,
            "source_tables": [
                AXIS_SOURCE,
                {
                    **_minute_source(template, ["close"]),
                },
            ],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": [101]},
            "stk",
            "1min",
            get_freq_step_count("1min"),
        ),
        FormulaBatch.from_text(
            common_inputs="close=get_hf('stk','1min','close')",
            formulas={"alpha": "factor=close"},
        ),
    )

    with pytest.raises(DataProviderError, match="Minute parquet file is missing"):
        BatchFactorEngine(provider).compute(request)


def test_stk_to_cb_mapping_uses_the_frozen_reordered_stock_axis() -> None:
    """验证股债映射使用冻结且已重排的股票资产轴。"""
    provider = _projection_provider()
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": [33, 11], "cb": [101, 102, 103]},
            "cb",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="stock_close = source('stk.1d.ClosePrice')",
            formulas={
                "alpha": (
                    "stock_signal = ts_mean(stock_close, 1)\n"
                    "factor = project_stk_to_cb(stock_signal)"
                )
            },
        ),
    )

    whole = BatchFactorEngine(provider).compute(request)
    chunked = BatchFactorEngine(provider).compute(
        request, options=ExecutionOptions(chunk_size=1)
    )

    expected = np.array(
        [
            [[10.0], [30.0], [float("nan")]],
            [[11.0], [31.0], [float("nan")]],
        ]
    )
    assert whole.domain.codes.tolist() == [101, 102, 103]
    assert whole.arrays["alpha"].shape == (2, 3, 1)
    np.testing.assert_allclose(whole.arrays["alpha"], expected, equal_nan=True)
    np.testing.assert_allclose(
        chunked.arrays["alpha"], whole.arrays["alpha"], equal_nan=True
    )
    mapping = next(
        term
        for term in whole.plan.terms.values()
        if isinstance(term, SourceTerm)
        and term.source_ref.logical_key == "cb.1d.underlying_stk"
    )
    assert mapping.value_kind is ValueKind.CODE
    assert any(
        isinstance(term, OperatorTerm) and term.operator_name == "lookup_by_col"
        for term in whole.plan.terms.values()
    )


def test_include_tables_mounts_only_whitelisted_source_tables(tmp_path) -> None:
    """验证 include_tables 白名单按 name 挂载指定表，未挂载的分钟源不可解析。"""
    minute = {**_minute_source(str(tmp_path / "{date}.parquet"), ["close"]), "name": "stk_1min"}
    axis = {
        **AXIS_SOURCE,
        "name": "stk_daily",
        "fields": [*AXIS_SOURCE["fields"], "ClosePrice"],
    }
    config = {
        "schema_version": 3,
        "source_tables": [axis, minute],
        "sources": {},
    }
    provider = SmartQuantDataProvider(
        backend=_Reader(),
        source_config=config,
        include_tables=["stk_daily"],
    )

    assert "stk.1d.ClosePrice" in provider.catalog.sources
    assert not any(key.startswith("stk.1min.") for key in provider.catalog.sources)
    with pytest.raises(DataProviderError, match="Unknown source"):
        provider.describe_many([SourceRefExpr.create("stk.1min.close")])


def test_include_tables_filters_named_sources_and_fundamentals(tmp_path) -> None:
    """验证白名单同时过滤 sources 段逻辑源与基本面自动注册。"""
    axis = {
        **AXIS_SOURCE,
        "name": "stk_daily",
        "fields": [*AXIS_SOURCE["fields"], "ClosePrice"],
    }
    underlying = {
        "asset": "cb",
        "freq": "1d",
        "source": "CBStockMap",
        "table": "JYDB.Bond_ConBDBasicInfo",
        "reader": "cb_stock_map",
        "field": "StockInnerCode",
        "value_kind": "code",
        "params": {"projection": "axis_position"},
    }
    config = {
        "schema_version": 3,
        "source_tables": [axis],
        "sources": {"cb.1d.underlying_stk": underlying},
    }
    # 白名单只含轴表：sources 段的映射源与基本面均不挂载。
    backend = _Reader()
    provider = SmartQuantDataProvider(
        backend=backend, source_config=config, include_tables=["stk_daily"]
    )
    assert set(provider.catalog.sources) == {"stk.1d.ClosePrice"}
    assert not any("Fundamental_ItemCode" in sql for sql in backend.sql)

    # 白名单显式包含 sources 逻辑键与 fundamentals 伪名字时才挂载。
    backend = _Reader()
    provider = SmartQuantDataProvider(
        backend=backend,
        source_config=config,
        include_tables=["stk_daily", "cb.1d.underlying_stk", "fundamentals"],
    )
    assert set(provider.catalog.sources) == {
        "stk.1d.ClosePrice",
        "cb.1d.underlying_stk",
    }
    assert any("Fundamental_ItemCode" in sql for sql in backend.sql)


def test_include_tables_rejects_unknown_table_names() -> None:
    """验证白名单中的未知名字直接报错而不是静默忽略。"""
    with pytest.raises(DataProviderError, match="unknown source table names"):
        SmartQuantDataProvider(
            backend=_Reader(),
            source_config={
                "schema_version": 3,
                "source_tables": [{**AXIS_SOURCE, "name": "stk_daily"}],
                "sources": {},
            },
            include_tables=["stk_daily", "nope"],
        )


def test_underlying_stk_projections_share_one_load_group() -> None:
    """验证位置与 inner_code 两种投影共用同一加载组且只查询一次。"""
    provider = _projection_provider()
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": [33, 11], "cb": [101, 102, 103]},
            "cb",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="stock_close = source('stk.1d.ClosePrice')",
            formulas={
                "alpha": "factor = project_stk_to_cb(stock_close)",
                "raw": "factor = source('cb.1d.underlying_stk', projection='inner_code')",
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    # 两种投影是同一逻辑键的两个独立 SourceTerm，不被 DAG 合并。
    mapping_terms = [
        term
        for term in result.plan.terms.values()
        if isinstance(term, SourceTerm)
        and term.source_ref.logical_key == "cb.1d.underlying_stk"
    ]
    assert len(mapping_terms) == 2
    assert {
        dict(term.source_ref.params).get("projection", "axis_position")
        for term in mapping_terms
    } == {"axis_position", "inner_code"}
    # projection 是字段级参数，不拆加载组：整个任务只查询一次映射表。
    assert (
        sum("Bond_ConBDBasicInfo" in sql for sql in provider.backend.sql) == 1
    )
    # 位置投影经 lookup_by_col 产出股票特征在转债轴上的映射。
    np.testing.assert_allclose(
        result.arrays["alpha"],
        np.array(
            [
                [[10.0], [30.0], [float("nan")]],
                [[11.0], [31.0], [float("nan")]],
            ]
        ),
        equal_nan=True,
    )
    # inner_code 投影保留任务无关的原始 StockInnerCode 并沿日期广播。
    np.testing.assert_allclose(
        result.arrays["raw"],
        np.array(
            [
                [[11.0], [33.0], [22.0]],
                [[11.0], [33.0], [22.0]],
            ]
        ),
    )


class _SecuCodeReader:
    """模拟日历、股票资产轴、InnerCode↔SecuCode 映射与 SecuCode 物理表的后端。"""

    def __init__(self) -> None:
        """初始化 SQL 记录列表。"""
        self.sql: list[str] = []

    def query(self, sql: str) -> pd.DataFrame:
        """记录 SQL 并按查询内容返回模拟数据。"""
        self.sql.append(sql)
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["ItemCode", "ItemName"])
        if "JY_TradingDayNew" in sql:
            return pd.DataFrame({"TradingDate": ["20240102", "20240103"]})
        if "InnerCode_SecuCode" in sql:
            return pd.DataFrame(
                {"InnerCode": [101, 202], "SecuCode": ["000001", "000002"]}
            )
        if "SELECT DISTINCT" in sql:
            return pd.DataFrame({"InnerCode": [101, 202]})
        if "FROM `Test`.`SecuDaily`" in sql:
            assert "IN ('000001', '000002')" in sql
            return pd.DataFrame(
                {
                    "DataDate": ["20240102", "20240102", "20240103"],
                    "InnerCode": ["000001", "000003", "000002"],
                    "value_0": [1.0, 9.9, 4.0],
                }
            )
        raise AssertionError(sql)


def _ops_dataset(template: str) -> dict[str, object]:
    """构造 SecuCode 代码列的日频 parquet 面板配置。"""
    return {
        "name": "ops_daily",
        "asset": "stk",
        "freq": "1d",
        "source": "OpsData",
        "table": template,
        "reader": "parquet_panel",
        "path_template": template,
        "asset_axis": False,
        "date_col": "DataDate",
        "date_col_type": "date",
        "code_col": "SecuCode",
        "code_identity": "secu_code",
        "fields": ["DataDate", "SecuCode", "close", "ret"],
    }


def _write_ops_parquet(directory, rows_by_date: dict[str, pd.DataFrame]) -> str:
    """按日期写出日频面板 parquet 文件并返回路径模板。"""
    for date_key, rows in rows_by_date.items():
        rows.to_parquet(directory / f"{date_key}.parquet")
    return str(directory / "{date}.parquet")


def test_parquet_panel_reads_secucode_daily_files(tmp_path) -> None:
    """验证日频 parquet 面板经 SecuCode 映射进入 InnerCode 内部协议。"""
    template = _write_ops_parquet(
        tmp_path,
        {
            "20240102": pd.DataFrame(
                {
                    "DataDate": ["2024-01-02"] * 3,
                    "SecuCode": ["000001", "000002", "000003"],
                    "close": [1.0, 2.0, 9.9],
                    "ret": [0.1, 0.2, 0.9],
                }
            ),
            "20240103": pd.DataFrame(
                {
                    "DataDate": ["2024-01-03"] * 2,
                    "SecuCode": ["000002", "000001"],
                    "close": [4.0, 3.0],
                    "ret": [0.4, 0.3],
                }
            ),
        },
    )
    backend = _SecuCodeReader()
    provider = SmartQuantDataProvider(
        backend=backend,
        source_config={
            "schema_version": 3,
            "source_tables": [AXIS_SOURCE, _ops_dataset(template)],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "close = source('stk.1d.close')\nret = source('stk.1d.ret')"
            ),
            formulas={"alpha": "factor = close + ret"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    # 000003 不在映射表中，被静默丢弃；两个字段同属一个加载组。
    np.testing.assert_allclose(
        result.arrays["alpha"],
        np.array([[[1.1], [2.2]], [[3.3], [4.4]]]),
    )
    assert sum("InnerCode_SecuCode" in sql for sql in backend.sql) == 1


def test_parquet_panel_fails_when_partition_file_is_missing(tmp_path) -> None:
    """验证日频面板任一分区文件缺失时加载组整体报错。"""
    template = _write_ops_parquet(
        tmp_path,
        {
            "20240102": pd.DataFrame(
                {
                    "DataDate": ["2024-01-02"],
                    "SecuCode": ["000001"],
                    "close": [1.0],
                    "ret": [0.1],
                }
            )
        },
    )
    provider = SmartQuantDataProvider(
        backend=_SecuCodeReader(),
        source_config={
            "schema_version": 3,
            "source_tables": [AXIS_SOURCE, _ops_dataset(template)],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={"alpha": "factor = close"},
        ),
    )

    with pytest.raises(DataProviderError, match="Daily parquet file is missing"):
        BatchFactorEngine(provider).compute(request)


def test_panel_fields_translates_secucode_identity() -> None:
    """验证 secu_code 身份的 SQL 表按映射过滤并翻译回 InnerCode。"""
    provider = SmartQuantDataProvider(
        backend=_SecuCodeReader(),
        source_config={
            "schema_version": 3,
            "source_tables": [
                AXIS_SOURCE,
                {
                    "name": "secu_daily",
                    "asset": "stk",
                    "freq": "1d",
                    "source": "SecuDaily",
                    "table": "Test.SecuDaily",
                    "reader": "sql_reader",
                    "query_builder": "panel_fields",
                    "asset_axis": False,
                    "date_col": "DataDate",
                    "code_col": "SecuCode",
                    "code_identity": "secu_code",
                    "fields": ["DataDate", "SecuCode", "close"],
                },
            ],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={"alpha": "factor = close"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    # SecuCode 000003 未映射被丢弃；其余按 InnerCode 轴对齐。
    np.testing.assert_allclose(
        result.arrays["alpha"],
        np.array([[[1.0], [np.nan]], [[np.nan], [4.0]]]),
        equal_nan=True,
    )


def test_code_map_is_frozen_once_per_task_across_partitions(tmp_path) -> None:
    """验证代码映射随资产轴在编译期冻结，分块执行下只查询一次。"""
    template = _write_ops_parquet(
        tmp_path,
        {
            "20240102": pd.DataFrame(
                {
                    "DataDate": ["2024-01-02"],
                    "SecuCode": ["000001"],
                    "close": [1.0],
                    "ret": [0.1],
                }
            ),
            "20240103": pd.DataFrame(
                {
                    "DataDate": ["2024-01-03"],
                    "SecuCode": ["000001"],
                    "close": [3.0],
                    "ret": [0.3],
                }
            ),
        },
    )
    backend = _SecuCodeReader()
    provider = SmartQuantDataProvider(
        backend=backend,
        source_config={
            "schema_version": 3,
            "source_tables": [AXIS_SOURCE, _ops_dataset(template)],
                        "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={"alpha": "factor = close"},
        ),
    )

    result = BatchFactorEngine(provider).compute(
        request, options=ExecutionOptions(chunk_size=1)
    )

    np.testing.assert_allclose(
        result.arrays["alpha"], np.array([[[1.0], [np.nan]], [[3.0], [np.nan]]]),
        equal_nan=True,
    )
    # 两个日期分区共用编译期冻结的同一份映射快照。
    assert sum("InnerCode_SecuCode" in sql for sql in backend.sql) == 1


def test_secucode_asset_without_registered_code_map_fails(tmp_path) -> None:
    """验证挂载 secu_code 数据集的资产未登记代码映射时在冻结轴阶段报错。"""
    template = _write_ops_parquet(
        tmp_path,
        {
            "20240102": pd.DataFrame(
                {
                    "DataDate": ["2024-01-02"],
                    "SecuCode": ["000001"],
                    "close": [1.0],
                    "ret": [0.1],
                }
            )
        },
    )
    provider = SmartQuantDataProvider(
        backend=_SecuCodeReader(),
        source_config={
            "schema_version": 3,
            # idx 在注册表中有资产轴但没有代码映射。
            "source_tables": [{**_ops_dataset(template), "name": "idx_ops", "asset": "idx"}],
            "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240102", {"idx": "all"}, "idx", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('idx.1d.close')",
            formulas={"alpha": "factor = close"},
        ),
    )

    with pytest.raises(DataProviderError, match="no registered code map"):
        BatchFactorEngine(provider).compute(request)


def test_asset_axis_and_code_map_work_without_mounting_axis_table(tmp_path) -> None:
    """验证轴与代码映射来自注册表：只挂载 ops 面板即可完整计算。"""
    template = _write_ops_parquet(
        tmp_path,
        {
            "20240102": pd.DataFrame(
                {
                    "DataDate": ["2024-01-02"] * 2,
                    "SecuCode": ["000001", "000002"],
                    "close": [1.0, 2.0],
                    "ret": [0.1, 0.2],
                }
            )
        },
    )
    provider = SmartQuantDataProvider(
        backend=_SecuCodeReader(),
        source_config={
            "schema_version": 3,
            "source_tables": [_ops_dataset(template)],
            "sources": {},
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240102", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={"alpha": "factor = close"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_allclose(
        result.arrays["alpha"], np.array([[[1.0], [2.0]]])
    )
