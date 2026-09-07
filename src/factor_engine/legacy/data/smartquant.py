"""旧版实现：SmartQuant 外部数据源的读取器。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .alignment import date_bounds, feature_array, feature_from_frame
from .model import ExecutionScope, FeatureArray, SourceSpec, get_freq_step_values
from .sources import minute_data_type, minute_path
from .store import FeatureStore
from ...data_provider.backend import OceanBaseBackend


def _duckdb_identifier(name: str) -> str:
    """在不改变字段语义的前提下引用 DuckDB 标识符。"""
    return '"' + str(name).replace('"', '""') + '"'


def _duckdb_literal(value: Any) -> str:
    """安全引用 SQL 字符串字面量。"""
    return "'" + str(value).replace("'", "''") + "'"


def _duckdb_literal_list(values: list[str]) -> str:
    """安全引用一组 DuckDB 字符串字面量。"""
    return "[" + ", ".join(_duckdb_literal(value) for value in values) + "]"


def _minute_code_map_path(store: FeatureStore, asset: str) -> Path:
    """返回指定资产在 Snapshot 中的代码映射 Parquet 路径。"""
    assets = store.manifest.get("assets", {})
    if asset not in assets:
        raise KeyError(f"Asset axis {asset!r} not found in snapshot")
    return store.root / assets[asset]["code_map_path"]


def _duckdb_threads(spec: SourceSpec) -> int:
    """解析分钟数据读取时单个 DuckDB 连接的线程数。"""
    try:
        requested = int(spec.params.get("duckdb_threads", 8))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "MinuteParquet param 'duckdb_threads' must be an integer"
        ) from exc
    return max(1, requested)


UNTRADABLE_COLUMNS = (
    "IfSpecialTrade",
    "IfSuspended",
    "IfNewListed",
    "IfLimitup",
    "IfLimitup_",
    "IfLimitdown",
    "IfLimitdown_",
    "IfSuspendedNextday",
    "IfLimitupNextday",
    "IfLimitup_Nextday",
    "IfLimitdownNextday",
    "IfLimitdown_Nextday",
    "IfLimitupNext5day",
    "IfLimitup_Next5day",
    "IfLimitdownNext5day",
    "IfLimitdown_Next5day",
)


@dataclass
class SmartQuantSourceReader:
    """将 SmartQuant 数据源规格读取为临时特征数组。"""

    user: str | None = None
    password: str | None = None
    host: str | None = None
    port: int | None = None

    def read_source(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """按 SourceSpec.source 分派读取逻辑并返回临时 FeatureArray。"""
        # source 类型是唯一分派依据，各读取器负责自身参数校验与对齐。
        source = spec.source
        if source == "ReturnDaily":
            return self._daily_field(
                spec, store, table=spec.table or "SmartQuant.ReturnDaily", scope=scope
            )
        if source == "CBReturnDaily":
            return self._daily_field(
                spec, store, table=spec.table or "SmartQuant.CBReturnDaily", scope=scope
            )
        if source == "IndexQuote":
            return self._index_quote_field(
                spec, store, table=spec.table or "JYDB.QT_IndexQuote", scope=scope
            )
        if source == "Fundamental":
            return self._fundamental(spec, store, scope=scope)
        if source == "Signal":
            return self._signal(spec, store, scope=scope)
        if source == "MinuteParquet":
            return self._minute_field(spec, store, scope=scope)
        if source == "Untradable":
            return self._untradable(spec, store, scope=scope)
        if source == "AdjustFactor":
            return self._adjust_factor(spec, store, scope=scope)
        if source == "IndexComponentWeight_Choice":
            return self._index_component(spec, store, scope=scope)
        if source == "CBStockMap":
            return self._cb_stock_map(spec, store, scope=scope)
        raise NotImplementedError(
            f"Source {source!r} is not supported by SmartQuantSourceReader"
        )

    def _fundamental(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取基本面季度字段并按 EndDateRank 对齐步长。"""
        # 解析日期范围和基本面表定位参数。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        field = spec.field or spec.name
        column_name = spec.params.get("column_name")
        quarters = int(spec.params.get("quarters") or 1)
        data_code = spec.params.get("data_code")
        publ_date_limit = int(spec.params.get("publ_date_limit", -180))
        if column_name is None:
            raise ValueError(f"Fundamental source {spec.key} requires column_name")
        # 未显式给出 ItemCode 时按字段名查询并固化到本次规格。
        if data_code is None:
            data_code = self._resolve_fundamental_data_code(field)
            spec = replace(spec, params={**spec.params, "data_code": data_code})
        sql = f"""
            SELECT DataDate, InnerCode, EndDateRank, {column_name} AS value
            FROM SmartQuant.Fundamental_Item{int(data_code)}
            WHERE DataDate BETWEEN '{start}' AND '{end}'
              AND InfoPublDate >= DATE_ADD(EndDate, INTERVAL {publ_date_limit} DAY)
              AND EndDateRank <= {quarters}
        """
        df = self._read_sql(sql)
        metadata = {
            "data_code": data_code,
            "column_name": column_name,
            "quarters": quarters,
            "publ_date_limit": publ_date_limit,
        }
        # 空结果仍返回形状完整的缺失数组。
        if df.empty:
            values = np.full(
                (space.n_dates, space.n_assets, quarters), np.nan, dtype=float
            )
            return feature_array(spec, store, values, metadata=metadata, scope=scope)
        # EndDateRank 从近到远编号，转换为数组中从旧到新的 step 位置。
        df = df.copy()
        df["step"] = quarters - df["EndDateRank"].astype(int)
        return feature_from_frame(
            spec,
            store,
            df,
            value_col="value",
            step_col="step",
            step_count=quarters,
            metadata=metadata,
            scope=scope,
        )

    def _resolve_fundamental_data_code(self, field: str) -> int:
        """按字段名查询唯一基本面 ItemCode。"""
        # 字段不存在或对应多个 ItemCode 时拒绝静默猜测。
        sql = f"""
            SELECT ItemCode, ItemName, ItemNameCN
            FROM SmartQuant.Fundamental_ItemCode
            WHERE ItemName = '{field}'
        """
        rows = self._read_sql(sql)
        if rows.empty:
            raise KeyError(
                f"Fundamental field {field!r} not found in SmartQuant.Fundamental_ItemCode"
            )
        if len(rows) > 1:
            candidates = rows[["ItemCode", "ItemNameCN"]].to_dict("records")
            raise ValueError(
                f"Fundamental field {field!r} is ambiguous; specify data_code. Candidates: {candidates}"
            )
        return int(rows.iloc[0]["ItemCode"])

    def _daily_field(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        table: str,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取并对齐股票或转债日频字段。"""
        # 查询有效交易日记录后交给公共长表对齐逻辑。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        field = spec.field or spec.name
        sql = f"""
            SELECT DataDate, InnerCode, {field} AS value
            FROM {table}
            WHERE DataDate BETWEEN '{start}' AND '{end}'
              AND IfTradingDay = 1
        """
        df = self._read_sql(sql)
        return feature_from_frame(spec, store, df, value_col="value", scope=scope)

    def _signal(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取信号表并把 SecuCode 映射到 Snapshot InnerCode。"""
        # 信号表按 runner_id 分表，查询后再使用快照代码映射。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        runner_id = int(spec.params["runner_id"])
        sql = f"""
            SELECT v.runner_date AS DataDate, v.runner_code AS SecuCode, v.runner_value AS value
            FROM Signal.runner_value_{runner_id} v
            WHERE v.runner_date BETWEEN '{start}' AND '{end}'
        """
        df = self._read_sql(sql)
        # 空表跳过映射，非空表将外部证券代码转换为固定资产轴代码。
        if not df.empty:
            df = store.to_inner_code(
                spec.asset,
                df,
                date_col="DataDate",
                code_col="SecuCode",
                errors="ignore",
            )
        return feature_from_frame(
            spec,
            store,
            df,
            value_col="value",
            metadata={"runner_id": runner_id},
            scope=scope,
        )

    def _minute_field(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """按天读取分钟 parquet，在 DuckDB 内完成代码映射并对齐为三维数组。"""
        # DuckDB 是分钟 parquet 路径的可选运行时依赖。
        try:
            import duckdb
        except ImportError as exc:
            raise ImportError("MinuteParquet source reading requires duckdb") from exc
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        field = spec.field or spec.name
        data_type = spec.params.get("data_type") or minute_data_type(spec.freq)
        path_template = spec.params.get("path_template")
        # 只读取实际存在的日期文件，全部缺失时给出明确路径错误。
        paths = [minute_path(date, data_type, path_template) for date in space.dates]
        existing_paths = [path.as_posix() for path in paths if path.exists()]
        if not existing_paths:
            raise FileNotFoundError(
                f"No minute parquet rows found for {spec.key} in {data_type!r} "
                f"between {start} and {end}. Check path_template or mounted minute data."
            )

        # 为目标日期、资产和 step 构造紧凑的位置轴。
        code_map_path = _minute_code_map_path(store, spec.asset)
        step_values = get_freq_step_values(spec.freq)
        values = np.full(space.shape, np.nan, dtype=float)
        field_sql = _duckdb_identifier(str(field))
        step_filter = ", ".join(str(int(value)) for value in step_values)
        date_col = str(spec.params.get("date_col", "trading_day"))
        date_col_type = str(spec.params.get("date_col_type", "date")).lower()
        date_sql = _duckdb_identifier(date_col)
        date_axis_values = ", ".join(
            f"({_duckdb_literal(str(date))}, {date_idx})"
            for date_idx, date in enumerate(space.dates)
        )
        asset_axis_values = ", ".join(
            f"({_duckdb_literal(str(code))}, {asset_idx})"
            for asset_idx, code in enumerate(space.codes)
        )
        step_axis_values = ", ".join(
            f"({int(step)}, {step_idx})" for step_idx, step in enumerate(step_values)
        )
        # 根据 parquet 日期列类型生成无歧义的过滤与规范键表达式。
        if date_col_type in {"date", "datetime", "timestamp"}:
            date_key_sql = f"strftime({date_sql}, '%Y%m%d')"
            date_filter_sql = f"{date_sql} BETWEEN DATE {_duckdb_literal(start)} AND DATE {_duckdb_literal(end)}"
        elif date_col_type in {"int", "integer", "yyyymmdd_int"}:
            date_key_sql = f"CAST({date_sql} AS VARCHAR)"
            date_filter_sql = (
                f"{date_sql} BETWEEN {int(space.dates[0])} AND {int(space.dates[-1])}"
            )
        else:
            date_key_sql = f"replace(CAST({date_sql} AS VARCHAR), '-', '')"
            date_filter_sql = f"{date_key_sql} BETWEEN {_duckdb_literal(str(space.dates[0]))} AND {_duckdb_literal(str(space.dates[-1]))}"
        parquet_sources = _duckdb_literal_list(existing_paths)
        # 在 DuckDB 内完成代码映射和三个坐标轴的位置连接。
        sql = f"""
            WITH
            date_axis(date_key, date_idx) AS (
                VALUES {date_axis_values}
            ),
            asset_axis(inner_code_key, asset_idx) AS (
                VALUES {asset_axis_values}
            ),
            step_axis(start_time, step_idx) AS (
                VALUES {step_axis_values}
            ),
            code_map AS (
                SELECT
                    d.date_key,
                    d.date_idx,
                    a.asset_idx,
                    c.SecuCode
                FROM read_parquet({_duckdb_literal(code_map_path.as_posix())}) c
                INNER JOIN date_axis d
                    ON CAST(c.DataDate AS VARCHAR) = d.date_key
                INNER JOIN asset_axis a
                    ON CAST(c.InnerCode AS VARCHAR) = a.inner_code_key
            ),
            minute_rows AS (
                SELECT
                    {date_key_sql} AS date_key,
                    security_code,
                    start_time,
                    {field_sql} AS value
                FROM read_parquet({parquet_sources})
                WHERE start_time IN ({step_filter})
                  AND {date_filter_sql}
            )
            SELECT
                c.date_idx AS date_idx,
                c.asset_idx AS asset_idx,
                s.step_idx AS step_idx,
                m.value AS value
            FROM minute_rows m
            INNER JOIN code_map c
                ON c.date_key = CAST(m.date_key AS VARCHAR)
               AND CAST(m.security_code AS VARCHAR) = CAST(c.SecuCode AS VARCHAR)
            INNER JOIN step_axis s
                ON m.start_time = s.start_time
        """
        # 每次读取使用短生命周期内存连接，并限制可配置线程数。
        conn = duckdb.connect(database=":memory:")
        try:
            conn.execute("SET enable_progress_bar=false")
            conn.execute(f"SET threads={_duckdb_threads(spec)}")
            data = conn.execute(sql).fetchnumpy()
        finally:
            conn.close()

        # 查询结果已是整数位置，仅需校验值类型并散布进预分配数组。
        if len(data.get("date_idx", ())) > 0:
            try:
                raw_values = np.asarray(data["value"], dtype=float)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"MinuteParquet field {field!r} for {spec.key} must be numeric"
                ) from exc

            date_idx = np.asarray(data["date_idx"], dtype=np.intp)
            asset_idx = np.asarray(data["asset_idx"], dtype=np.intp)
            step_idx = np.asarray(data["step_idx"], dtype=np.intp)
            values[date_idx, asset_idx, step_idx] = raw_values

        return feature_array(
            spec,
            store,
            values,
            metadata={
                "data_type": data_type,
                "path_template": path_template,
                "date_col": date_col,
            },
            scope=scope,
        )

    def _untradable(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取 SmartQuant.Untradable 并合成 is_untradable 布尔特征。"""
        # 多个不可交易标志在 SQL 端合并为单个布尔字段。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        table = spec.table or "SmartQuant.Untradable"
        columns = tuple(
            str(col) for col in spec.params.get("columns", UNTRADABLE_COLUMNS)
        )
        if not columns:
            raise ValueError("Untradable source requires at least one flag column")
        flag_expr = " OR ".join(f"COALESCE({col}, 0) = 1" for col in columns)
        sql = f"""
            SELECT
                DataDate,
                InnerCode,
                CASE WHEN {flag_expr} THEN TRUE ELSE FALSE END AS value
            FROM {table}
            WHERE DataDate BETWEEN '{start}' AND '{end}'
        """
        df = self._read_sql(sql)
        return feature_from_frame(
            spec,
            store,
            df,
            value_col="value",
            dtype=bool,
            missing_value=False,
            metadata={"columns": list(columns)},
            scope=scope,
        )

    def _adjust_factor(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取股票复权因子并按日期资产轴对齐。"""
        # 每个交易日取不晚于当日的最新复权记录，缺省因子为一。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        sql = f"""
            SELECT
                s.DataDate,
                s.InnerCode,
                COALESCE((
                    SELECT a.RatioAdjustingFactor
                    FROM JYDB.DZ_AdjustingFactor a
                    WHERE a.InnerCode = s.InnerCode
                      AND s.TradingDay >= a.ExDiviDate
                    ORDER BY a.ExDiviDate DESC
                    LIMIT 1
                ), 1) AS value
            FROM SmartQuant.ReturnDaily s
            WHERE s.DataDate BETWEEN '{start}' AND '{end}'
              AND s.IfTradingDay = 1
        """
        df = self._read_sql(sql)
        return feature_from_frame(spec, store, df, value_col="value", scope=scope)

    def _index_quote_field(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        table: str,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取指数日频行情字段。"""
        # 指数行情使用 TradingDay 作为公共对齐层需要的 DataDate。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        field = spec.field or spec.name
        sql = f"""
            SELECT TradingDay AS DataDate, InnerCode, {field} AS value
            FROM {table}
            WHERE TradingDay BETWEEN '{start}' AND '{end}'
        """
        df = self._read_sql(sql)
        return feature_from_frame(spec, store, df, value_col="value", scope=scope)

    def _index_component(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取指数成分权重，并按配置返回权重或成员布尔值。"""
        # 指数可按内部代码或外部代码二选一过滤。
        space = store.resolve_space(spec, scope=scope)
        start, end = date_bounds(space)
        index_filter = (
            f"IndexInnerCode = {int(spec.params['index_inner_code'])}"
            if "index_inner_code" in spec.params
            else f"IndexCode = '{spec.params['index_code']}'"
        )
        sql = f"""
            SELECT EndDate AS DataDate, SecuInnerCode AS InnerCode, Weight
            FROM SmartQuant.IndexComponentWeight_Choice
            WHERE EndDate BETWEEN '{start}' AND '{end}'
              AND {index_filter}
        """
        df = self._read_sql(sql)
        # 成员语义转换为布尔掩码，否则保留原始权重数值。
        if spec.params.get("kind") == "index_membership":
            df = df.copy()
            df["value"] = True
            return feature_from_frame(
                spec,
                store,
                df,
                value_col="value",
                dtype=bool,
                missing_value=False,
                scope=scope,
            )
        df = df.rename(columns={"Weight": "value"})
        return feature_from_frame(spec, store, df, value_col="value", scope=scope)

    def _cb_stock_map(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """读取转债正股映射，并生成正股 InnerCode 或股票轴列号。"""
        # 基础映射与日期无关，先一次性查询转债到正股代码关系。
        space = store.resolve_space(spec, scope=scope)
        sql = """
            SELECT T1.InnerCode, T2.StockInnerCode AS value
            FROM JYDB.Bond_Code T1
            INNER JOIN JYDB.Bond_ConBDBasicInfo T2
                ON T1.InnerCode = T2.InnerCode
            WHERE T1.BondNature IN (10, 29)
        """
        df = self._read_sql(sql)
        df = df.copy()
        # 调用方可选择保留正股内部代码，或转换成股票资产轴位置。
        if (
            spec.params.get("kind") == "inner_code"
            or spec.name == "underlying_stk_inner_code"
        ):
            df["value"] = (
                pd.to_numeric(df["value"], errors="coerce").fillna(-1).astype(np.int64)
            )
            dtype = np.int64
        else:
            stock_col = {
                code: pos for pos, code in enumerate(store.get_asset_codes("stk"))
            }
            df["value"] = df["value"].map(stock_col).fillna(-1).astype(np.int32)
            dtype = np.int32
        # 将静态映射扩展到请求中的每个日期后复用公共对齐函数。
        rows = []
        for date in space.dates:
            tmp = df.copy()
            tmp["DataDate"] = date
            rows.append(tmp)
        expanded = (
            pd.concat(rows, ignore_index=True)
            if rows
            else pd.DataFrame(columns=["DataDate", "InnerCode", "value"])
        )
        return feature_from_frame(
            spec,
            store,
            expanded,
            value_col="value",
            dtype=dtype,
            missing_value=-1,
            scope=scope,
        )

    def _read_sql(self, sql: str) -> pd.DataFrame:
        """通过新数据层的公共 OceanBase backend 执行兼容读取。"""
        return OceanBaseBackend(self.user, self.password, self.host, self.port).query(
            sql
        )
