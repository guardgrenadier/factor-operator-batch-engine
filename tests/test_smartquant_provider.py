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
    ReadDomain,
    SmartQuantDataProvider,
    SourceBinding,
    SourceRefExpr,
    SourceSpec,
    SourceTerm,
    ValueKind,
)
from factor_engine.domain import get_freq_step_count, get_freq_step_values
from factor_engine.data_provider.backend import DuckDBBackend, sql_literal_list
from factor_engine.data_provider.normalize import scatter_positions, scatter_rows


AXIS_SOURCE = {
    "asset": "stk",
    "freq": "1d",
    "source": "ReturnDaily",
    "table": "SmartQuant.ReturnDaily",
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
        "path_template": template,
        "date_col": "trading_day",
        "date_col_type": "date",
        "fields": [
            "trading_day",
            "security_code",
            "start_time",
            *fields,
        ],
    }


def _legacy_minute_arrays(
    paths, code_map: pd.DataFrame, bindings: tuple[SourceBinding, ...]
) -> dict[str, np.ndarray]:
    """用旧版逐行散点方式为分钟数据源基准值数组。"""
    domain = bindings[0].read_domain
    legacy_map = code_map.copy()
    legacy_map["date_key"] = legacy_map["DataDate"]
    legacy_map["date_idx"] = legacy_map["date_key"].map(
        {date: idx for idx, date in enumerate(domain.dates)}
    )
    legacy_map["asset_idx"] = legacy_map["InnerCode"].map(
        {code: idx for idx, code in enumerate(domain.codes)}
    )
    step_axis = pd.DataFrame(
        {"start_time": domain.steps, "step_idx": range(len(domain.steps))}
    )
    aliases = {
        binding.term_id: f"value_{position}"
        for position, binding in enumerate(bindings)
    }
    select = ", ".join(
        f'p."{binding.source_spec.field}" AS "{aliases[binding.term_id]}"'
        for binding in bindings
    )
    rows = DuckDBBackend().query(
        "SELECT m.date_idx, m.asset_idx, s.step_idx, "
        + select
        + f" FROM read_parquet({sql_literal_list([path.as_posix() for path in paths])}) p "
        "INNER JOIN code_map m ON strftime(p.trading_day, '%Y%m%d') = m.date_key "
        "AND CAST(p.security_code AS VARCHAR) = CAST(m.SecuCode AS VARCHAR) "
        "INNER JOIN step_axis s ON p.start_time = s.start_time",
        tables={"code_map": legacy_map, "step_axis": step_axis},
    )
    return scatter_positions(bindings, rows, aliases)


def _projection_provider() -> SmartQuantDataProvider:
    """构造含股票、可转债与股债映射源的投影数据提供方。"""
    return SmartQuantDataProvider(
        backend=_ProjectionReader(),
        source_config={
            "schema_version": 2,
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
                    "asset_axis": True,
                    "date_col": "DataDate",
                    "trading_flag_col": "IfTradingDay",
                    "fields": ["DataDate", "InnerCode", "IfTradingDay"],
                },
            ],
            "sources": {
                "cb.1d.underlying_stk_col": {
                    "asset": "cb",
                    "freq": "1d",
                    "source": "CBStockMap",
                    "table": "JYDB.Bond_ConBDBasicInfo",
                    "field": "StockInnerCode",
                    "value_kind": "code",
                    "params": {"kind": "col"},
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
            "schema_version": 2,
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
            "schema_version": 2,
            "source_tables": [AXIS_SOURCE],
            "sources": {},
        },
    )

    codes = provider.asset_codes("stk", ["20240103", "20240104"], [202, 101])

    assert codes.tolist() == [202, 101]


def test_wide_and_asset_axis_support_custom_code_col() -> None:
    """验证宽表与资产轴支持自定义资产编码列。"""
    reader = _CodeColReader()
    provider = SmartQuantDataProvider(
        backend=reader,
        source_config={
            "schema_version": 2,
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

    axis_sql = next(sql for sql in reader.sql if "SELECT DISTINCT" in sql)
    assert "`SecurityCode` AS InnerCode" in axis_sql
    assert "ORDER BY `SecurityCode`" in axis_sql
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
        if caller == "factor_engine.data_provider.datasets":
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
            "schema_version": 2,
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
    assert "cast_to_type(m.SecuCode, p.security_code)" in duckdb.minute_sql
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
            "schema_version": 2,
            "source_tables": [
                {
                    "asset": "cb",
                    "freq": "1d",
                    "source": "CBReturnDaily",
                    "table": "SmartQuant.CBReturnDaily",
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
    """验证分钟流式读取的形状、类型、数值与旧版逐行对齐一致。"""
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
            "schema_version": 2,
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

    # 用旧版读取域与数据源绑定重建逐行散点基线数组。
    domain = ReadDomain(
        ("20240102", "20240103"),
        ("20240102", "20240103"),
        (202, 101),
        tuple(get_freq_step_values("1min")),
        slice(0, 2),
    )
    bindings = tuple(
        SourceBinding(
            name,
            SourceSpec("stk", "1min", name, field=name),
            domain,
            "minute",
        )
        for name in ("close", "volume")
    )
    legacy = _legacy_minute_arrays(
        [tmp_path / "20240102.parquet", tmp_path / "20240103.parquet"],
        code_map,
        bindings,
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
            "schema_version": 2,
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


def test_scatter_uses_existing_value_column_and_membership_constant() -> None:
    """验证逐行散点复用已有 value 列并填充成员常量。"""
    domain = ReadDomain(
        ("20240102",),
        ("20240102",),
        (101, 202),
        (0,),
        slice(0, 1),
    )
    weight = SourceBinding(
        "weight",
        SourceSpec("stk", "1d", "weight", params={"kind": "index_weight"}),
        domain,
        "index",
    )
    member = SourceBinding(
        "member",
        SourceSpec(
            "stk", "1d", "member", params={"kind": "index_membership"}
        ),
        domain,
        "index",
        ValueKind.MASK,
    )
    rows = pd.DataFrame(
        {
            "DataDate": ["20240102"],
            "InnerCode": [101],
            "value": [0.25],
        }
    )

    result = scatter_rows(
        (weight, member),
        rows,
        {"weight": "value"},
        constants={"member": 1.0},
        defaults={"member": 0.0},
    )

    np.testing.assert_allclose(
        result["weight"][:, :, 0], [[0.25, np.nan]], equal_nan=True
    )
    np.testing.assert_array_equal(result["member"][:, :, 0], [[1.0, 0.0]])
    assert rows.columns.tolist() == ["value"]


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
            "schema_version": 2,
            "source_tables": [
                AXIS_SOURCE,
                {
                    "asset": "stk",
                    "freq": "1min",
                    "source": "MinuteParquet",
                    "table": template,
                    "path_template": template,
                    "date_col": "trading_day",
                    "fields": [
                        "trading_day",
                        "security_code",
                        "start_time",
                        "close",
                    ],
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
        and term.source_ref.logical_key == "cb.1d.underlying_stk_col"
    )
    assert mapping.value_kind is ValueKind.CODE
    assert any(
        isinstance(term, OperatorTerm) and term.operator_name == "lookup_by_col"
        for term in whole.plan.terms.values()
    )
