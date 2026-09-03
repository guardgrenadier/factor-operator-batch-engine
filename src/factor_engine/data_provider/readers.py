"""具名 Reader：执行物理读取并流式返回 RawBatch，不做最终数组协议。

Reader 只负责执行物理读取与解释结果坐标；坐标散布、dtype、缺失、
ValueKind 与默认值全部由 LoadNormalizer（normalize.py）独占。
代码身份（InnerCode/SecuCode）转换也在 Reader 层完成：内部协议始终是
InnerCode，SecuCode 源在取数时经资产的 code_map 翻译后进入 RawBatch。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd

from ..domain import normalize_date_key
from ..model import DataProviderError, SourceBinding
from .backend import (
    DuckDBBackend,
    column,
    duckdb_identifier,
    integer_list,
    measured_arrow,
    measured_query,
    sql_identifier,
    sql_literal,
    sql_literal_list,
    sql_table,
)
from .catalog import minute_path
from .normalize import normalize_batches
from .query_builders import SQL_QUERY_BUILDERS

if TYPE_CHECKING:
    from .catalog import Catalog


Emit = Callable[..., None]


@dataclass(frozen=True)
class RawBatch:
    """Reader 产出的原始批次：坐标列或位置提示加原始值列。

    mode 为坐标模式：labels（DataDate/InnerCode/可选 Step 标签）、
    flat（已映射到读取域的 flat_idx 整数位置）、static（无日期，仅 InnerCode）、
    dense（坐标已与 ReadDomain 对齐的完整数组，不做散布，只授权值协议）。
    frame 对 labels/static 是 DataFrame，对 flat 是 Arrow RecordBatch，
    对 dense 是 term_id 到 ndarray 的映射。
    """

    mode: str
    frame: Any


@dataclass(frozen=True)
class ReaderRequest:
    """一次加载组读取的全部上下文：绑定、目录、后端、任务轴与诊断回调。

    axes 与 code_maps 都是任务编译期冻结的事实：资产轴、以及有
    secu_code 数据源资产的 InnerCode↔SecuCode 映射，Reader 只取用。
    """

    bindings: tuple[SourceBinding, ...]
    catalog: Catalog
    backend: Any
    duckdb: DuckDBBackend
    axes: Mapping[str, np.ndarray]
    code_maps: Mapping[str, pd.DataFrame]
    emit: Emit


def load_group(
    bindings: Sequence[SourceBinding],
    catalog: Catalog,
    sql_backend: Any,
    duckdb: DuckDBBackend,
    axes: Mapping[str, np.ndarray],
    code_maps: Mapping[str, pd.DataFrame],
    emit: Emit,
) -> Mapping[str, np.ndarray]:
    """按 DatasetSpec 的具名 Reader 读取，并交给 LoadNormalizer 规范化。"""
    first = bindings[0].source_spec
    try:
        reader = READER_REGISTRY[str(first.reader)]
    except KeyError as exc:
        raise DataProviderError(f"Unsupported dataset reader {first.reader!r}") from exc
    request = ReaderRequest(
        tuple(bindings), catalog, sql_backend, duckdb, axes, code_maps, emit
    )
    return normalize_batches(request.bindings, reader(request))


def query_code_map(
    backend: Any,
    map_spec: Mapping[str, Any],
    axis_spec: Mapping[str, Any],
    dates: Sequence[Any],
    codes: Sequence[Any],
    emit: Emit,
) -> pd.DataFrame:
    """物理查询某资产的 InnerCode↔SecuCode 映射，供轴冻结时调用。

    map_spec 由 Provider 的代码注册表显式登记：{"table": ...} 静态映射表，
    或 {"from_asset_axis": true} 按日期从资产轴表取（dated 映射，结果带
    DataDate 列，轴表参数来自 axis_spec）。dates 必须覆盖含 lookback 的
    完整读取视野。
    """
    table = map_spec.get("table")
    if table is not None:
        return measured_query(
            lambda: backend.query(
                f"SELECT InnerCode, SecuCode FROM {sql_table(str(table))} "
                f"WHERE InnerCode IN ({integer_list(codes)})"
            ),
            emit,
            operation="code_map",
            dataset=str(table),
            fields=("SecuCode",),
        )
    if not map_spec.get("from_asset_axis"):
        raise DataProviderError(
            "code map spec must declare 'table' or 'from_asset_axis'"
        )
    # dated 映射：从资产轴表按读取视野日期取 InnerCode/SecuCode。
    code_col = str(axis_spec["code_col"])
    filters = [
        f"{sql_identifier(axis_spec['date_col'])} BETWEEN {sql_literal(dates[0])} "
        f"AND {sql_literal(dates[-1])}",
        f"{sql_identifier(code_col)} IN ({integer_list(codes)})",
    ]
    if axis_spec["trading_flag_col"]:
        filters.append(f"{sql_identifier(axis_spec['trading_flag_col'])} = 1")
    return measured_query(
        lambda: backend.query(
            f"SELECT {sql_identifier(axis_spec['date_col'])} AS DataDate, "
            f"{sql_identifier(code_col)} AS InnerCode, SecuCode "
            f"FROM {sql_table(axis_spec['table'])} "
            f"WHERE {' AND '.join(filters)}"
        ),
        emit,
        operation="code_map",
        dataset=axis_spec["table"],
        fields=("SecuCode",),
    )


def frozen_code_map(request: ReaderRequest, asset: str) -> pd.DataFrame:
    """取任务编译期随资产轴冻结的代码映射，缺失说明 Provider 未准备。"""
    try:
        return request.code_maps[asset]
    except KeyError as exc:
        raise DataProviderError(
            f"Code map for asset {asset!r} is not frozen"
        ) from exc


def sql_reader(request: ReaderRequest) -> Iterator[RawBatch]:
    """执行具名 Query Builder 生成的规范 labels SQL 并包装为 RawBatch。"""
    first = request.bindings[0].source_spec
    try:
        builder = SQL_QUERY_BUILDERS[str(first.query_builder)]
    except KeyError as exc:
        raise DataProviderError(
            f"Unsupported SQL query builder {first.query_builder!r}"
        ) from exc
    # SecuCode 身份的物理表：builder 用冻结映射翻译过滤值，
    # 查询结果在这里翻译回 InnerCode 内部协议。
    code_map = None
    if str(first.params.get("code_identity", "inner_code")) == "secu_code":
        code_map = frozen_code_map(request, first.asset)
    aliases = _aliases(request.bindings)
    sql, fields = builder(request, aliases)
    rows = measured_query(
        lambda: request.backend.query(sql),
        request.emit,
        operation="load",
        dataset=first.table or "",
        fields=fields,
        domain=request.bindings[0].read_domain,
    )
    if code_map is not None and not rows.empty:
        rows = _translate_secucode_rows(rows, code_map)
    yield RawBatch("labels", rows)


def _translate_secucode_rows(rows: pd.DataFrame, code_map: pd.DataFrame) -> pd.DataFrame:
    """把 labels 结果中的 SecuCode 代码列翻译回 InnerCode，丢弃未映射行。"""
    code_name = column(rows, "InnerCode")
    secucode = rows[code_name].map(str)
    if "DataDate" in code_map.columns:
        mapping = {
            (normalize_date_key(date), str(secu)): int(inner)
            for date, secu, inner in zip(
                code_map["DataDate"],
                code_map["SecuCode"],
                code_map["InnerCode"],
                strict=True,
            )
        }
        keys = [
            (normalize_date_key(date), str(secu))
            for date, secu in zip(rows[column(rows, "DataDate")], secucode)
        ]
        mapped = pd.array([mapping.get(key) for key in keys], dtype="Int64")
    else:
        mapping = {
            str(secu): int(inner)
            for secu, inner in zip(
                code_map["SecuCode"], code_map["InnerCode"], strict=True
            )
        }
        mapped = pd.array([mapping.get(value) for value in secucode], dtype="Int64")
    keep = pd.notna(mapped)
    translated = rows.loc[keep].copy()
    translated[code_name] = mapped[keep]
    return translated


def fundamental(request: ReaderRequest) -> Iterator[RawBatch]:
    """基本面长表：执行 rank/PIT 查询并把报告期 rank 解码为 Step 坐标。"""
    first, domain = request.bindings[0].source_spec, request.bindings[0].read_domain
    quarters = int(first.params["quarters"])
    aliases = _aliases(request.bindings)
    # 构造要读取的基本面字段 SELECT 子句。
    select = ", ".join(
        f"{sql_identifier(str(binding.source_spec.params['column_name']))} "
        f"AS {sql_identifier(aliases[binding.term_id])}"
        for binding in request.bindings
    )
    # 执行物理查询：日期、资产、披露日与时滞以及报告期排名过滤。
    rows = measured_query(
        lambda: request.backend.query(
            "SELECT DataDate, InnerCode, EndDateRank, "
            f"{select} FROM {sql_table(first.table or '')} "
            f"WHERE DataDate BETWEEN {sql_literal(domain.dates[0])} "
            f"AND {sql_literal(domain.dates[-1])} "
            f"AND InnerCode IN ({integer_list(domain.codes)}) "
            "AND InfoPublDate >= DATE_ADD(EndDate, INTERVAL "
            f"{int(first.params['publ_date_limit'])} DAY) "
            f"AND EndDateRank <= {quarters}"
        ),
        request.emit,
        operation="load",
        dataset=first.table or "",
        fields=[str(b.source_spec.params["column_name"]) for b in request.bindings],
        domain=domain,
    )
    # 把报告期排名换算成 step（越早排名越小、step 越大），并重命名为规范列。
    if not rows.empty:
        rows["Step"] = quarters - pd.to_numeric(
            rows[column(rows, "EndDateRank")], errors="raise"
        ).astype(int)
        rows.drop(columns=[column(rows, "EndDateRank")], inplace=True)
    yield RawBatch("labels", rows)


def cb_stock_map(request: ReaderRequest) -> Iterator[RawBatch]:
    """转债正股关系：无日期的 static 结果及任务股票轴位置投影。"""
    domain = request.bindings[0].read_domain
    rows = measured_query(
        lambda: request.backend.query(
            "SELECT T1.InnerCode, T2.StockInnerCode AS value "
            "FROM JYDB.Bond_Code T1 INNER JOIN JYDB.Bond_ConBDBasicInfo T2 "
            "ON T1.InnerCode = T2.InnerCode WHERE T1.BondNature IN (10, 29) "
            f"AND T1.InnerCode IN ({integer_list(domain.codes)})"
        ),
        request.emit,
        operation="load",
        dataset="JYDB.Bond_ConBDBasicInfo",
        fields=["StockInnerCode"],
        domain=domain,
    )
    if rows.empty:
        yield RawBatch("static", rows)
        return
    raw = pd.to_numeric(rows[column(rows, "value")], errors="raise")
    aliases = _aliases(request.bindings)
    frame = pd.DataFrame({column(rows, "InnerCode"): rows[column(rows, "InnerCode")]})
    # 逐绑定投影：axis_position 依赖本任务已冻结的股票资产轴，找不到时为 NaN。
    for binding in request.bindings:
        projection = binding.source_spec.params.get("projection", "inner_code")
        if projection == "axis_position":
            if "stk" not in request.axes:
                raise DataProviderError(
                    f"{binding.source_spec.key} requires the task stock asset axis"
                )
            positions = {
                int(code): col
                for col, code in enumerate(np.asarray(request.axes["stk"]).tolist())
            }
            frame[aliases[binding.term_id]] = raw.map(positions)
        elif projection == "inner_code":
            frame[aliases[binding.term_id]] = raw
        else:
            raise DataProviderError(
                f"Unknown cb_stock_map projection {projection!r}"
            )
    yield RawBatch("static", frame)


def parquet_bars(request: ReaderRequest) -> Iterator[RawBatch]:
    """按日期分区的 parquet bars：流式返回已映射为 flat_idx 位置的批次。"""
    first, domain = request.bindings[0].source_spec, request.bindings[0].read_domain
    dataset = request.catalog.datasets[str(first.params["dataset_id"])]
    paths = [minute_path(dataset, date) for date in domain.dates]
    code_col = str(first.params.get("code_col", "InnerCode"))
    identity = str(first.params.get("code_identity", "inner_code"))
    # 构造文件、资产、step 三个内存轴表，用于把物理行定位到坐标。
    tables: dict[str, pd.DataFrame] = {
        "file_axis": pd.DataFrame(
            {
                "filename": [path.as_posix() for path in paths],
                "date_key": list(domain.dates),
                "date_idx": np.arange(len(paths), dtype=np.int64),
            }
        ),
        "asset_axis": pd.DataFrame(
            {
                "InnerCode": list(domain.codes),
                "asset_idx": np.arange(len(domain.codes), dtype=np.int64),
            }
        ),
        "step_axis": pd.DataFrame(
            {"start_time": list(domain.steps), "step_idx": range(len(domain.steps))}
        ),
    }
    # 代码列身份为 secu_code 时经任务冻结的 code_map 翻译回 InnerCode。
    if identity == "secu_code":
        code_map = frozen_code_map(request, dataset["asset"])
        tables["code_map"] = code_map
        date_join = (
            "replace(substr(CAST(m.DataDate AS VARCHAR), 1, 10), '-', '') "
            "= f.date_key AND "
            if "DataDate" in code_map.columns
            else ""
        )
        code_join = (
            f"INNER JOIN code_map m ON {date_join}"
            f"p.{duckdb_identifier(code_col)} "
            f"= cast_to_type(m.SecuCode, p.{duckdb_identifier(code_col)}) "
            "INNER JOIN asset_axis a ON m.InnerCode "
            "= cast_to_type(a.InnerCode, m.InnerCode) "
        )
    else:
        code_join = (
            f"INNER JOIN asset_axis a ON p.{duckdb_identifier(code_col)} "
            f"= cast_to_type(a.InnerCode, p.{duckdb_identifier(code_col)}) "
        )
    # 构造字段 SELECT 与计算扁平坐标的关联 SQL；Infinity 由 LoadNormalizer 处理。
    aliases = _aliases(request.bindings)
    select = ", ".join(
        f"CAST(p.{duckdb_identifier(binding.source_spec.field or binding.source_spec.name)} "
        f"AS DOUBLE) AS {duckdb_identifier(aliases[binding.term_id])}"
        for binding in request.bindings
    )
    size = len(domain.codes) * len(domain.steps)
    sql = (
        f"SELECT CAST(f.date_idx * {size} + a.asset_idx * {len(domain.steps)} "
        " + s.step_idx AS BIGINT) AS flat_idx, "
        + select
        + f" FROM read_parquet({sql_literal_list([path.as_posix() for path in paths])}, filename=true) p "
        "INNER JOIN file_axis f ON p.filename = f.filename "
        + code_join
        + "INNER JOIN step_axis s ON p.start_time = cast_to_type(s.start_time, p.start_time)"
    )
    batches = measured_arrow(
        lambda: request.duckdb.iter_arrow(
            sql,
            tables=tables,
            threads=dataset["duckdb_threads"],
        ),
        request.emit,
        operation="load",
        dataset=dataset["table"],
        fields=[binding.source_spec.field for binding in request.bindings],
        domain=domain,
    )
    try:
        for batch in batches:
            yield RawBatch("flat", batch)
    except DataProviderError:
        raise
    except Exception as exc:
        missing = next((path for path in paths if not path.exists()), None)
        if missing is not None:
            raise DataProviderError(f"Minute parquet file is missing: {missing}") from exc
        raise
    finally:
        batches.close()


def parquet_panel(request: ReaderRequest) -> Iterator[RawBatch]:
    """按日期分区的日频 parquet 面板：流式返回 flat_idx 位置批次（S=1）。

    与 parquet_bars 的区别是没有 step 轴：日期来自文件内日期列，
    代码列身份由 code_identity 声明（inner_code 直接关联资产轴，
    secu_code 经资产的 code_map 翻译）。
    """
    first, domain = request.bindings[0].source_spec, request.bindings[0].read_domain
    if len(domain.steps) != 1:
        raise DataProviderError(
            "parquet_panel requires a single-step (daily) ReadDomain"
        )
    dataset = request.catalog.datasets[str(first.params["dataset_id"])]
    paths = [minute_path(dataset, date) for date in domain.dates]
    date_col = str(first.params.get("date_col", "DataDate"))
    code_col = str(first.params.get("code_col", "InnerCode"))
    identity = str(first.params.get("code_identity", "inner_code"))
    # 构造文件与资产两个内存轴表，用于把物理行定位到坐标。
    tables: dict[str, pd.DataFrame] = {
        "file_axis": pd.DataFrame(
            {
                "date_key": list(domain.dates),
                "date_idx": np.arange(len(paths), dtype=np.int64),
            }
        ),
        "asset_axis": pd.DataFrame(
            {
                "InnerCode": list(domain.codes),
                "asset_idx": np.arange(len(domain.codes), dtype=np.int64),
            }
        ),
    }
    date_join = (
        f"replace(substr(CAST(p.{duckdb_identifier(date_col)} AS VARCHAR), 1, 10), "
        "'-', '') = f.date_key"
    )
    if identity == "secu_code":
        code_map = frozen_code_map(request, dataset["asset"])
        tables["code_map"] = code_map
        if "DataDate" in code_map.columns:
            date_join += (
                " AND replace(substr(CAST(m.DataDate AS VARCHAR), 1, 10), "
                "'-', '') = f.date_key"
            )
        code_join = (
            f"INNER JOIN code_map m ON p.{duckdb_identifier(code_col)} "
            f"= cast_to_type(m.SecuCode, p.{duckdb_identifier(code_col)}) "
            "INNER JOIN asset_axis a ON m.InnerCode "
            "= cast_to_type(a.InnerCode, m.InnerCode)"
        )
    else:
        code_join = (
            f"INNER JOIN asset_axis a ON p.{duckdb_identifier(code_col)} "
            f"= cast_to_type(a.InnerCode, p.{duckdb_identifier(code_col)})"
        )
    aliases = _aliases(request.bindings)
    select = ", ".join(
        f"CAST(p.{duckdb_identifier(binding.source_spec.field or binding.source_spec.name)} "
        f"AS DOUBLE) AS {duckdb_identifier(aliases[binding.term_id])}"
        for binding in request.bindings
    )
    sql = (
        f"SELECT CAST(f.date_idx * {len(domain.codes)} + a.asset_idx AS BIGINT) "
        f"AS flat_idx, {select} "
        f"FROM read_parquet({sql_literal_list([path.as_posix() for path in paths])}, "
        "filename=true) p "
        f"INNER JOIN file_axis f ON {date_join} {code_join}"
    )
    batches = measured_arrow(
        lambda: request.duckdb.iter_arrow(
            sql,
            tables=tables,
            threads=dataset["duckdb_threads"],
        ),
        request.emit,
        operation="load",
        dataset=dataset["table"],
        fields=[binding.source_spec.field for binding in request.bindings],
        domain=domain,
    )
    try:
        for batch in batches:
            yield RawBatch("flat", batch)
    except DataProviderError:
        raise
    except Exception as exc:
        missing = next((path for path in paths if not path.exists()), None)
        if missing is not None:
            raise DataProviderError(f"Daily parquet file is missing: {missing}") from exc
        raise
    finally:
        batches.close()


def _aliases(bindings: Sequence[SourceBinding]) -> dict[str, str]:
    """为每个绑定按顺序生成稳定的 value_序号 结果列别名。"""
    return {
        binding.term_id: f"value_{position}"
        for position, binding in enumerate(bindings)
    }


READER_REGISTRY: dict[str, Callable[[ReaderRequest], Iterator[RawBatch]]] = {
    "sql_reader": sql_reader,
    "fundamental": fundamental,
    "parquet_bars": parquet_bars,
    "parquet_panel": parquet_panel,
    "cb_stock_map": cb_stock_map,
}
