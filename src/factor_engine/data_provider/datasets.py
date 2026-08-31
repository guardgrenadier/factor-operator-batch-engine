"""按物理读取布局实现的最小 Reader 函数注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import numpy as np
import pandas as pd

from ..model import DataProviderError, DatasetSpec, RawBatch, ReaderRequest, SourceSpec
from .backend import (
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


Reader = Callable[[ReaderRequest], Iterator[RawBatch]]


@dataclass(frozen=True)
class SQLQuery:
    """Query Builder 交给统一 SQL Reader 的最小执行描述。"""

    sql: str
    fields: tuple[Any, ...]
    dataset: str | None = None


QueryBuilder = Callable[[ReaderRequest, Mapping[str, str]], SQLQuery]


def minute_path(dataset: DatasetSpec, date: Any) -> Path:
    """渲染单个日期分区的 parquet 路径。"""

    template = str(dataset.params["path_template"])
    date_key = str(date).replace("-", "")[:8]
    return Path(template.format(date=date_key))


def panel_fields(
    request: ReaderRequest, aliases: Mapping[str, str]
) -> SQLQuery:
    """构造 date + asset + 多字段或常量投影的面板 SQL。"""

    dataset, bindings, domain = request.dataset, request.bindings, request.read_domain
    params = dataset.params
    date_col, code_col = str(params["date_col"]), str(params["code_col"])
    # 常量 binding 不占物理 SELECT 列，只在实际返回的行上由 _labels_batch 投影。
    projections = [
        f"{sql_identifier(str(binding.source_spec.field))} "
        f"AS {sql_identifier(aliases[binding.term_id])}"
        for binding in bindings
        if binding.source_spec.field is not None
    ]
    # 行集合只由 Dataset 配置、ReadDomain 和共享 selector 决定。
    filters = [
        f"{sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])}",
        f"{sql_identifier(code_col)} IN ({integer_list(domain.codes)})",
    ]
    trading_col = params.get("trading_flag_col")
    if trading_col:
        filters.append(f"{sql_identifier(str(trading_col))} = 1")
    selector = params.get("selector")
    if selector:
        param_name = str(selector["param"])
        selected = _shared_param(bindings, param_name)
        literal = (
            str(int(selected))
            if selector.get("type") == "integer"
            else sql_literal(selected)
        )
        filters.append(f"{sql_identifier(str(selector['column']))} = {literal}")
    select = ", ".join(
        [
            f"{sql_identifier(date_col)} AS DataDate",
            f"{sql_identifier(code_col)} AS InnerCode",
            *projections,
        ]
    )
    return SQLQuery(
        f"SELECT {select} FROM {sql_table(str(params['table']))} "
        f"WHERE {' AND '.join(filters)}",
        tuple(
            binding.source_spec.field
            for binding in bindings
            if binding.source_spec.field
        ),
    )


def fundamental(request: ReaderRequest) -> Iterator[RawBatch]:
    """读取带披露过滤和报告期 rank 的 SQL 面板并解码 step。"""

    dataset, bindings, domain = request.dataset, request.bindings, request.read_domain
    params = dataset.params
    quarters = int(_shared_param(bindings, "quarters"))
    publ_date_limit = int(_shared_param(bindings, "publ_date_limit"))
    aliases = _aliases(bindings)
    projections = ", ".join(
        f"{sql_identifier(str(binding.source_spec.field))} "
        f"AS {sql_identifier(aliases[binding.term_id])}"
        for binding in bindings
    )
    date_col, code_col = str(params["date_col"]), str(params["code_col"])
    rank_col = str(params["rank_col"])
    report_col = str(params["report_date_col"])
    publication_col = str(params["publication_date_col"])
    # PIT 披露窗口和 rank 上限决定物理行集合，因此也是 LoadGroup 兼容条件。
    rows = _query(
        request,
        f"SELECT {sql_identifier(date_col)} AS DataDate, "
        f"{sql_identifier(code_col)} AS InnerCode, "
        f"{sql_identifier(rank_col)} AS EndDateRank, {projections} "
        f"FROM {sql_table(str(params['table']))} "
        f"WHERE {sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND {sql_identifier(code_col)} IN ({integer_list(domain.codes)}) "
        f"AND {sql_identifier(publication_col)} >= "
        f"DATE_ADD({sql_identifier(report_col)}, INTERVAL {publ_date_limit} DAY) "
        f"AND {sql_identifier(rank_col)} <= {quarters}",
        [binding.source_spec.field for binding in bindings],
    )
    if rows.empty:
        return
    # Reader 只把物理 rank 解码为 step 标签，最终 S 轴校验留给 Normalizer。
    rank_name = column(rows, "EndDateRank")
    step = quarters - pd.to_numeric(rows[rank_name], errors="raise").astype(int)
    yield _labels_batch(rows, bindings, aliases, step=step)


def adjust_factor(
    request: ReaderRequest, aliases: Mapping[str, str]
) -> SQLQuery:
    """构造每个交易行最近生效除权日的 as-of SQL。"""

    if len(request.bindings) != 1:
        raise DataProviderError("adjust_factor requires one binding")
    binding, domain, params = request.bindings[0], request.read_domain, request.dataset.params
    anchor: DatasetSpec = request.context["anchor_dataset"]
    anchor_params = anchor.params
    date_col = str(anchor_params["date_col"])
    code_col = str(anchor_params["code_col"])
    trading_col = str(anchor_params["trading_flag_col"])
    # as-of 业务规则固定在具名 Query Builder，不向配置暴露任意 SQL 模板。
    alias = sql_identifier(aliases[binding.term_id])
    return SQLQuery(
        f"SELECT s.{sql_identifier(date_col)} AS DataDate, "
        f"s.{sql_identifier(code_col)} AS InnerCode, COALESCE(("
        f"SELECT a.RatioAdjustingFactor FROM {sql_table(str(params['factor_table']))} a "
        f"WHERE a.InnerCode = s.{sql_identifier(code_col)} "
        "AND s.`TradingDay` >= a.ExDiviDate "
        f"ORDER BY a.ExDiviDate DESC LIMIT 1), 1) AS {alias} "
        f"FROM {sql_table(str(anchor_params['table']))} s "
        f"WHERE s.{sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND s.{sql_identifier(code_col)} IN ({integer_list(domain.codes)}) "
        f"AND s.{sql_identifier(trading_col)} = 1",
        ("RatioAdjustingFactor",),
        str(params["factor_table"]),
    )


def untradable(
    request: ReaderRequest, aliases: Mapping[str, str]
) -> SQLQuery:
    """构造一组具名交易状态 OR 得到不可交易 mask 的 SQL。"""

    if len(request.bindings) != 1:
        raise DataProviderError("untradable requires one binding")
    binding, domain, params = (
        request.bindings[0],
        request.read_domain,
        request.dataset.params,
    )
    # 状态列集合是具名派生规则；缺失物理行的默认值由 SourceSpec 声明。
    flags = " OR ".join(
        f"COALESCE({sql_identifier(name)}, 0) = 1" for name in UNTRADABLE_COLUMNS
    )
    date_col, code_col = str(params["date_col"]), str(params["code_col"])
    alias = sql_identifier(aliases[binding.term_id])
    return SQLQuery(
        f"SELECT {sql_identifier(date_col)} AS DataDate, "
        f"{sql_identifier(code_col)} AS InnerCode, "
        f"CASE WHEN {flags} THEN 1 ELSE 0 END AS {alias} "
        f"FROM {sql_table(str(params['table']))} "
        f"WHERE {sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND {sql_identifier(code_col)} IN ({integer_list(domain.codes)})",
        UNTRADABLE_COLUMNS,
    )


def sql_reader(request: ReaderRequest) -> Iterator[RawBatch]:
    """统一执行具名 Query Builder，并返回规范 labels RawBatch。"""

    builder_name = request.dataset.query_builder
    if builder_name is None:
        raise DataProviderError("sql_reader requires a query_builder")
    try:
        builder = SQL_QUERY_BUILDERS[builder_name]
    except KeyError as exc:
        raise DataProviderError(f"Unknown SQL query builder {builder_name!r}") from exc
    aliases = _aliases(request.bindings)
    query = builder(request, aliases)
    rows = _query(
        request,
        query.sql,
        query.fields,
        dataset=query.dataset,
    )
    if not rows.empty:
        yield _labels_batch(rows, request.bindings, aliases)


def cb_stock_map(request: ReaderRequest) -> Iterator[RawBatch]:
    """读取可转债到正股的静态关系，并可投影为冻结股票轴位置。"""

    domain, params = request.read_domain, request.dataset.params
    rows = _query(
        request,
        "SELECT T1.InnerCode, T2.StockInnerCode AS value "
        f"FROM {sql_table(str(params['bond_code_table']))} T1 "
        f"INNER JOIN {sql_table(str(params['relation_table']))} T2 "
        "ON T1.InnerCode = T2.InnerCode WHERE T1.BondNature IN (10, 29) "
        f"AND T1.InnerCode IN ({integer_list(domain.codes)})",
        ("StockInnerCode",),
        dataset=str(params["relation_table"]),
    )
    if rows.empty:
        return
    raw = rows[column(rows, "value")]
    values: dict[str, Any] = {}
    for binding in request.bindings:
        if binding.source_spec.projection == "axis_position":
            # 轴位置是任务级物理投影；原始 inner_code 则保持原列交给 Normalizer。
            target = str(binding.source_spec.params.get("target_asset", "stk"))
            try:
                axis = request.context["axes"][target]
            except KeyError as exc:
                raise DataProviderError(
                    f"{binding.source_spec.key} requires the task {target} asset axis"
                ) from exc
            positions = {
                int(code): position
                for position, code in enumerate(np.asarray(axis).tolist())
            }
            values[binding.term_id] = raw.map(
                lambda value: (
                    np.nan if pd.isna(value) else positions.get(int(value), np.nan)
                )
            )
        else:
            values[binding.term_id] = raw
    yield RawBatch(
        "static",
        {"asset": rows[column(rows, "InnerCode")]},
        values,
    )


def parquet_bars(request: ReaderRequest) -> Iterator[RawBatch]:
    """流式扫描日期分区 parquet，并返回稳定扁平坐标。"""

    dataset, bindings, domain = request.dataset, request.bindings, request.read_domain
    params = dataset.params
    paths = [minute_path(dataset, date) for date in domain.dates]
    code_map_config = params["code_map"]
    mode = str(code_map_config["mode"])
    sql_backend = request.context["sql_backend"]
    emit = request.context["emit"]
    # 代码映射是 parquet 扫描的内部物理依赖，不进入公式或 LoadGroup 身份。
    if mode == "static":
        code_map_table = str(code_map_config["table"])
        code_map = _direct_query(
            sql_backend,
            "SELECT `InnerCode`, `SecuCode` "
            f"FROM {sql_table(code_map_table)} "
            f"WHERE `InnerCode` IN ({integer_list(domain.codes)})",
            emit,
            code_map_table,
            ("SecuCode",),
            domain,
            operation="code_map",
        )
        date_join = ""
    elif mode == "dated":
        map_dataset: DatasetSpec = request.context["code_map_dataset"]
        map_params = map_dataset.params
        date_col, code_col = str(map_params["date_col"]), str(map_params["code_col"])
        filters = [
            f"{sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
            f"AND {sql_literal(domain.dates[-1])}",
            f"{sql_identifier(code_col)} IN ({integer_list(domain.codes)})",
        ]
        if map_params.get("trading_flag_col"):
            filters.append(f"{sql_identifier(str(map_params['trading_flag_col']))} = 1")
        code_map = _direct_query(
            sql_backend,
            f"SELECT {sql_identifier(date_col)} AS DataDate, "
            f"{sql_identifier(code_col)} AS InnerCode, "
            "`SecuCode` "
            f"FROM {sql_table(str(map_params['table']))} "
            f"WHERE {' AND '.join(filters)}",
            emit,
            str(map_params["table"]),
            ("SecuCode",),
            domain,
            operation="code_map",
        )
        date_join = (
            "replace(substr(CAST(m.DataDate AS VARCHAR), 1, 10), '-', '') "
            "= f.date_key AND "
        )
    else:
        raise DataProviderError(f"Unknown parquet code_map mode {mode!r}")

    # 四个任务级小表把文件、存储代码、资产和 step 映射到稳定位置。
    file_axis = pd.DataFrame(
        {
            "filename": [path.as_posix() for path in paths],
            "date_key": list(domain.dates),
            "date_idx": np.arange(len(paths), dtype=np.int64),
        }
    )
    asset_axis = pd.DataFrame(
        {
            "InnerCode": list(domain.codes),
            "asset_idx": np.arange(len(domain.codes), dtype=np.int64),
        }
    )
    step_axis = pd.DataFrame(
        {"step_value": list(domain.steps), "step_idx": range(len(domain.steps))}
    )
    aliases = _aliases(bindings)
    if any(binding.source_spec.field is None for binding in bindings):
        raise DataProviderError("parquet_bars sources require physical fields")
    select = ", ".join(
        f"p.{duckdb_identifier(str(binding.source_spec.field))} "
        f"AS {duckdb_identifier(aliases[binding.term_id])}"
        for binding in bindings
    )
    # DuckDB 在流式扫描时直接生成 C-order flat_idx，避免 Reader 分配最终数组。
    size = len(domain.codes) * len(domain.steps)
    sql = (
        f"SELECT CAST(f.date_idx * {size} + a.asset_idx * {len(domain.steps)} "
        " + s.step_idx AS BIGINT) AS flat_idx, "
        + select
        + f" FROM read_parquet({sql_literal_list([path.as_posix() for path in paths])}, filename=true) p "
        "INNER JOIN file_axis f ON p.filename = f.filename "
        f"INNER JOIN code_map m ON {date_join}"
        'p."security_code" = cast_to_type(m.SecuCode, p."security_code") '
        "INNER JOIN asset_axis a ON m.InnerCode = cast_to_type(a.InnerCode, m.InnerCode) "
        'INNER JOIN step_axis s ON p."start_time" = '
        'cast_to_type(s.step_value, p."start_time")'
    )
    duckdb = request.context["duckdb"]
    batches = measured_arrow(
        lambda: duckdb.iter_arrow(
            sql,
            tables={
                "code_map": code_map,
                "file_axis": file_axis,
                "asset_axis": asset_axis,
                "step_axis": step_axis,
            },
            threads=int(params.get("duckdb_threads", 8)),
        ),
        emit,
        operation="load",
        dataset=str(params["path_template"]),
        fields=[binding.source_spec.field for binding in bindings],
        domain=domain,
    )
    # Arrow batch 原样流入同一个 Normalizer，以保留跨 batch 重复检测。
    try:
        for batch in batches:
            yield RawBatch(
                "flat",
                {"flat_idx": batch.column(0)},
                {
                    binding.term_id: batch.column(position)
                    for position, binding in enumerate(bindings, 1)
                },
            )
    except DataProviderError:
        raise
    except Exception as exc:
        missing = next((path for path in paths if not path.exists()), None)
        if missing is not None:
            raise DataProviderError(f"Minute parquet file is missing: {missing}") from exc
        raise
    finally:
        batches.close()


def reader_compatibility(dataset: DatasetSpec, source: SourceSpec) -> tuple[Any, ...]:
    """返回影响物理行集合或坐标解码的 Reader 专属合批参数。"""

    if dataset.query_builder == "panel_fields" and dataset.params.get("selector"):
        name = str(dataset.params["selector"]["param"])
        return (name, source.params.get(name))
    if dataset.reader == "fundamental":
        return (
            int(source.params["quarters"]),
            int(source.params["publ_date_limit"]),
        )
    return ()


def _labels_batch(
    rows: pd.DataFrame,
    bindings: tuple,
    aliases: Mapping[str, str],
    *,
    step: Any | None = None,
) -> RawBatch:
    """把统一别名的 SQL 行包装为 labels RawBatch，并投影常量列。"""

    coordinates: dict[str, Any] = {
        "date": rows[column(rows, "DataDate")],
        "asset": rows[column(rows, "InnerCode")],
    }
    if step is not None:
        coordinates["step"] = step
    values = {
        binding.term_id: (
            np.full(len(rows), binding.source_spec.constant)
            if binding.source_spec.constant is not None
            else rows[column(rows, aliases[binding.term_id])]
        )
        for binding in bindings
    }
    return RawBatch("labels", coordinates, values)


def _aliases(bindings: tuple) -> dict[str, str]:
    """为 LoadGroup 的值列生成与 term_id 对应的稳定查询别名。"""

    return {
        binding.term_id: f"value_{position}"
        for position, binding in enumerate(bindings)
    }


def _shared_param(bindings: tuple, name: str) -> Any:
    """读取所有 binding 必须共享的 Reader 参数，否则拒绝合批。"""

    values = {binding.source_spec.params.get(name) for binding in bindings}
    if len(values) != 1 or None in values:
        raise DataProviderError(f"LoadGroup requires one shared {name!r}")
    return values.pop()


def _query(
    request: ReaderRequest,
    sql: str,
    fields: Any,
    *,
    dataset: str | None = None,
) -> pd.DataFrame:
    """用 ReaderRequest 中的后端和诊断上下文执行一次 SQL 查询。"""

    return _direct_query(
        request.context["sql_backend"],
        sql,
        request.context["emit"],
        dataset
        or str(request.dataset.params.get("table", request.dataset.dataset_id)),
        fields,
        request.read_domain,
    )


def _direct_query(
    backend: Any,
    sql: str,
    emit: Callable[..., None],
    dataset: str,
    fields: Any,
    domain: Any,
    *,
    operation: str = "load",
) -> pd.DataFrame:
    """执行带统一 operation、Dataset、字段和 ReadDomain 诊断的查询。"""

    return measured_query(
        lambda: backend.query(sql),
        emit,
        operation=operation,
        dataset=dataset,
        fields=fields,
        domain=domain,
    )


READER_REGISTRY: dict[str, Reader] = {
    "sql_reader": sql_reader,
    "fundamental": fundamental,
    "parquet_bars": parquet_bars,
    "cb_stock_map": cb_stock_map,
}

READER_MODES = {
    "sql_reader": "labels",
    "fundamental": "labels",
    "parquet_bars": "flat",
    "cb_stock_map": "static",
}

SQL_QUERY_BUILDERS: dict[str, QueryBuilder] = {
    "panel_fields": panel_fields,
    "adjust_factor": adjust_factor,
    "untradable": untradable,
}

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


__all__ = [
    "READER_MODES",
    "READER_REGISTRY",
    "SQL_QUERY_BUILDERS",
    "SQLQuery",
    "reader_compatibility",
]
