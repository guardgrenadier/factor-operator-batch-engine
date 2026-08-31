"""各物理数据集的 SQL 查询读取器（LoadGroup 分发与逐类型加载）。"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

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
from .catalog import Catalog, minute_path
from .normalize import scatter_rows, scatter_static


Emit = Callable[..., None]


def load_group(
    bindings: Sequence[SourceBinding],
    catalog: Catalog,
    sql_backend: Any,
    duckdb: DuckDBBackend,
    axes: Mapping[str, np.ndarray],
    emit: Emit,
) -> Mapping[str, np.ndarray]:
    """按数据集类型分派对应读取器，加载一个加载组内的全部绑定。"""

    source = bindings[0].source_spec.source
    loaders = {
        "ReturnDaily": _wide,
        "CBReturnDaily": _wide,
        "IndexQuote": _wide,
        "Fundamental": _fundamental,
        "Untradable": _untradable,
        "AdjustFactor": _adjust_factor,
        "IndexComponentWeight_Choice": _index_component,
        "CBStockMap": _cb_stock_map,
    }
    if source == "MinuteParquet":
        return _minute(bindings, catalog, sql_backend, duckdb, emit)
    try:
        loader = loaders[str(source)]
    except KeyError as exc:
        raise DataProviderError(f"Unsupported dataset reader {source!r}") from exc
    return loader(bindings, sql_backend, axes, emit)


def _wide(bindings, backend, axes, emit):
    """宽表行情数据集：按日期、资产（及交易日标志）过滤查询后散布。"""

    first, domain = bindings[0].source_spec, bindings[0].read_domain
    date_col = str(first.params["date_col"])
    code_col = str(first.params.get("code_col", "InnerCode"))
    aliases = _aliases(bindings)
    # 构造要读取的字段 SELECT 子句（按绑定别名命名）。
    select = ", ".join(
        f"{sql_identifier(binding.source_spec.field or binding.source_spec.name)} "
        f"AS {sql_identifier(aliases[binding.term_id])}"
        for binding in bindings
    )
    # 构造读取域的日期与资产过滤条件。
    filters = [
        f"{sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])}",
        f"{sql_identifier(code_col)} IN ({integer_list(domain.codes)})",
    ]
    trading_col = first.params.get("trading_flag_col")
    if trading_col:
        filters.append(f"{sql_identifier(str(trading_col))} = 1")
    # 执行物理查询并将长表散布到各绑定的共同坐标。
    rows = _query(
        backend,
        f"SELECT {sql_identifier(date_col)} AS DataDate, "
        f"{sql_identifier(code_col)} AS InnerCode, {select} "
        f"FROM {sql_table(first.table or '')} WHERE {' AND '.join(filters)}",
        first.table or "",
        [binding.source_spec.field for binding in bindings],
        domain,
        emit,
    )
    return scatter_rows(bindings, rows, aliases)


def _fundamental(bindings, backend, axes, emit):
    """基本面数据集：按报告期排名过滤查询，并把报告期排名换算为 step。"""

    first, domain = bindings[0].source_spec, bindings[0].read_domain
    quarters = int(first.params["quarters"])
    aliases = _aliases(bindings)
    # 构造要读取的基本面字段 SELECT 子句。
    select = ", ".join(
        f"{sql_identifier(str(binding.source_spec.params['column_name']))} "
        f"AS {sql_identifier(aliases[binding.term_id])}"
        for binding in bindings
    )
    # 执行物理查询：日期、资产、披露日与时滞以及报告期排名过滤。
    rows = _query(
        backend,
        "SELECT DataDate, InnerCode, EndDateRank, "
        f"{select} FROM {sql_table(first.table or '')} "
        f"WHERE DataDate BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND InnerCode IN ({integer_list(domain.codes)}) "
        "AND InfoPublDate >= DATE_ADD(EndDate, INTERVAL "
        f"{int(first.params['publ_date_limit'])} DAY) "
        f"AND EndDateRank <= {quarters}",
        first.table or "",
        [binding.source_spec.params["column_name"] for binding in bindings],
        domain,
        emit,
    )
    # 把报告期排名换算成 step（越早排名越小、step 越大），并去掉原列。
    if not rows.empty:
        rows["step"] = quarters - pd.to_numeric(
            rows[column(rows, "EndDateRank")], errors="raise"
        ).astype(int)
        rows.drop(columns=[column(rows, "EndDateRank")], inplace=True)
    return scatter_rows(bindings, rows, aliases, step_col="step")


def _untradable(bindings, backend, axes, emit):
    """不可交易数据集：用多个标志列的或运算派生单一 0/1 字段并散布。"""

    if len(bindings) != 1:
        raise DataProviderError("Untradable is one derived field")
    binding, domain = bindings[0], bindings[0].read_domain
    columns = tuple(binding.source_spec.params.get("columns", UNTRADABLE_COLUMNS))
    flags = " OR ".join(
        f"COALESCE({sql_identifier(str(name))}, 0) = 1" for name in columns
    )
    rows = _query(
        backend,
        "SELECT DataDate, InnerCode, "
        f"CASE WHEN {flags} THEN 1 ELSE 0 END AS value_0 "
        f"FROM {sql_table(binding.source_spec.table or '')} "
        f"WHERE DataDate BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND InnerCode IN ({integer_list(domain.codes)})",
        binding.source_spec.table or "",
        columns,
        domain,
        emit,
    )
    return scatter_rows(
        bindings,
        rows,
        {binding.term_id: "value_0"},
        defaults={binding.term_id: 0.0},
    )


def _adjust_factor(bindings, backend, axes, emit):
    """复权因子数据集：按最近除权日关联子查询派生复权因子并散布。"""

    if len(bindings) != 1:
        raise DataProviderError("AdjustFactor is one derived field")
    binding, domain = bindings[0], bindings[0].read_domain
    rows = _query(
        backend,
        "SELECT s.DataDate, s.InnerCode, COALESCE(("
        "SELECT a.RatioAdjustingFactor FROM JYDB.DZ_AdjustingFactor a "
        "WHERE a.InnerCode = s.InnerCode AND s.TradingDay >= a.ExDiviDate "
        "ORDER BY a.ExDiviDate DESC LIMIT 1), 1) AS value_0 "
        "FROM SmartQuant.ReturnDaily s "
        f"WHERE s.DataDate BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND s.InnerCode IN ({integer_list(domain.codes)}) AND s.IfTradingDay = 1",
        binding.source_spec.table or "JYDB.DZ_AdjustingFactor",
        ("RatioAdjustingFactor",),
        domain,
        emit,
    )
    return scatter_rows(bindings, rows, {binding.term_id: "value_0"})


def _index_component(bindings, backend, axes, emit):
    """指数成分权重数据集：查询权重，并区分成员关系与权重字段散布。"""

    first, domain = bindings[0].source_spec, bindings[0].read_domain
    index_filter = (
        f"IndexInnerCode = {int(first.params['index_inner_code'])}"
        if "index_inner_code" in first.params
        else f"IndexCode = {sql_literal(first.params['index_code'])}"
    )
    rows = _query(
        backend,
        "SELECT EndDate AS DataDate, SecuInnerCode AS InnerCode, Weight AS value "
        f"FROM {sql_table(first.table or '')} "
        f"WHERE EndDate BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND SecuInnerCode IN ({integer_list(domain.codes)}) AND {index_filter}",
        first.table or "",
        ("Weight",),
        domain,
        emit,
    )
    # 按绑定 kind 分类：成员关系用常量 1/0，其余读取权重列。
    fields, constants, defaults = {}, {}, {}
    for binding in bindings:
        if binding.source_spec.params.get("kind") == "index_membership":
            constants[binding.term_id] = 1.0
            defaults[binding.term_id] = 0.0
        else:
            fields[binding.term_id] = "value"
    return scatter_rows(
        bindings, rows, fields, constants=constants, defaults=defaults
    )


def _cb_stock_map(bindings, backend, axes, emit):
    """转债正股映射数据集：把正股代码换算为任务资产轴位置后静态散布。"""

    domain = bindings[0].read_domain
    rows = _query(
        backend,
        "SELECT T1.InnerCode, T2.StockInnerCode AS value "
        "FROM JYDB.Bond_Code T1 INNER JOIN JYDB.Bond_ConBDBasicInfo T2 "
        "ON T1.InnerCode = T2.InnerCode WHERE T1.BondNature IN (10, 29) "
        f"AND T1.InnerCode IN ({integer_list(domain.codes)})",
        "JYDB.Bond_ConBDBasicInfo",
        ("StockInnerCode",),
        domain,
        emit,
    )
    # 将正股代码映射为任务股票资产轴上的整数位置，其余读取原始列。
    fields, prepared = {}, {}
    raw = pd.to_numeric(rows[column(rows, "value")], errors="raise")
    for binding in bindings:
        if binding.source_spec.params.get("kind") == "col":
            if "stk" not in axes:
                raise DataProviderError(
                    f"{binding.source_spec.key} requires the task stock asset axis"
                )
            positions = {
                int(code): col
                for col, code in enumerate(np.asarray(axes["stk"]).tolist())
            }
            prepared[binding.term_id] = raw.map(positions)
        else:
            fields[binding.term_id] = "value"
    return scatter_static(bindings, rows, fields, prepared=prepared)


def _minute(bindings, catalog, sql_backend, duckdb, emit):
    """分钟 parquet 数据集：按文件、代码、step 轴关联 DuckDB 逐批读取。"""

    first, domain = bindings[0].source_spec, bindings[0].read_domain
    dataset = catalog.datasets[str(first.params["dataset_id"])]
    paths = [minute_path(dataset, date) for date in domain.dates]
    # 构造代码映射：股票按静态 InnerCode，其他资产按日期关联查询。
    if dataset["asset"] == "stk":
        code_map_table = "SmartQuant.InnerCode_SecuCode"
        code_map = _query(
            sql_backend,
            f"SELECT InnerCode, SecuCode FROM {sql_table(code_map_table)} "
            f"WHERE InnerCode IN ({integer_list(domain.codes)})",
            code_map_table,
            ("SecuCode",),
            domain,
            emit,
            operation="code_map",
        )
        date_join = ""
    else:
        axis_dataset = catalog.asset_datasets[dataset["asset"]]
        code_col = str(axis_dataset.get("code_col", "InnerCode"))
        filters = [
            f"{sql_identifier(axis_dataset['date_col'])} BETWEEN {sql_literal(domain.dates[0])} "
            f"AND {sql_literal(domain.dates[-1])}",
            f"{sql_identifier(code_col)} IN ({integer_list(domain.codes)})",
        ]
        if axis_dataset["trading_flag_col"]:
            filters.append(f"{sql_identifier(axis_dataset['trading_flag_col'])} = 1")
        code_map = _query(
            sql_backend,
            f"SELECT {sql_identifier(axis_dataset['date_col'])} AS DataDate, "
            f"{sql_identifier(code_col)} AS InnerCode, SecuCode "
            f"FROM {sql_table(axis_dataset['table'])} "
            f"WHERE {' AND '.join(filters)}",
            axis_dataset["table"],
            ("SecuCode",),
            domain,
            emit,
            operation="code_map",
        )
        date_join = (
            "replace(substr(CAST(m.DataDate AS VARCHAR), 1, 10), '-', '') "
            "= f.date_key AND "
        )
    # 构造文件、资产、step 三个内存轴表，用于把物理行定位到坐标。
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
        {"start_time": list(domain.steps), "step_idx": range(len(domain.steps))}
    )
    # 构造字段 SELECT（无限值转 NULL）与计算扁平坐标的关联 SQL。
    aliases = _aliases(bindings)
    select = ", ".join(
        f"IF(isinf(CAST(p.{duckdb_identifier(binding.source_spec.field or binding.source_spec.name)} "
        f"AS DOUBLE)), NULL, p.{duckdb_identifier(binding.source_spec.field or binding.source_spec.name)})"
        f"::DOUBLE AS {duckdb_identifier(aliases[binding.term_id])}"
        for binding in bindings
    )
    size = len(domain.codes) * len(domain.steps)
    sql = (
        f"SELECT CAST(f.date_idx * {size} + a.asset_idx * {len(domain.steps)} "
        " + s.step_idx AS BIGINT) AS flat_idx, "
        + select
        + f" FROM read_parquet({sql_literal_list([path.as_posix() for path in paths])}, filename=true) p "
        "INNER JOIN file_axis f ON p.filename = f.filename "
        f"INNER JOIN code_map m ON {date_join}"
        "p.security_code = cast_to_type(m.SecuCode, p.security_code) "
        "INNER JOIN asset_axis a ON m.InnerCode = cast_to_type(a.InnerCode, m.InnerCode) "
        "INNER JOIN step_axis s ON p.start_time = cast_to_type(s.start_time, p.start_time)"
    )
    shape = (len(domain.dates), len(domain.codes), len(domain.steps))
    total = len(domain.dates) * size
    result = {
        binding.term_id: np.full(shape, np.nan, dtype=np.float64)
        for binding in bindings
    }
    occupied = np.zeros(total, dtype=np.bool_)

    batches = measured_arrow(
        lambda: duckdb.iter_arrow(
            sql,
            tables={
                "code_map": code_map,
                "file_axis": file_axis,
                "asset_axis": asset_axis,
                "step_axis": step_axis,
            },
            threads=dataset["duckdb_threads"],
        ),
        emit,
        operation="load",
        dataset=dataset["table"],
        fields=[binding.source_spec.field for binding in bindings],
        domain=domain,
    )
    # 逐批消费 Arrow 结果，校验坐标唯一性并写入三维输出数组。
    try:
        for batch in batches:
            flat_idx = batch.column(0).to_numpy(zero_copy_only=False)
            if np.any((flat_idx < 0) | (flat_idx >= total)):
                raise DataProviderError("Backend returned position outside ReadDomain")
            ordered_idx = np.sort(flat_idx)
            if (
                np.any(ordered_idx[1:] == ordered_idx[:-1])
                or occupied[flat_idx].any()
            ):
                raise DataProviderError(
                    "Backend returned duplicate date/asset/step coordinates"
                )
            occupied[flat_idx] = True
            for position, binding in enumerate(bindings, 1):
                result[binding.term_id].reshape(-1)[flat_idx] = batch.column(
                    position
                ).to_numpy(
                    zero_copy_only=False
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
    # Runtime 在 Provider 交接边界统一校验 shape、dtype 和 ValueKind。
    return result


def _aliases(bindings):
    """为每个绑定按顺序生成稳定的 value_序号 结果列别名。"""

    return {
        binding.term_id: f"value_{position}"
        for position, binding in enumerate(bindings)
    }


def _query(
    backend,
    sql,
    dataset,
    fields,
    domain,
    emit,
    *,
    operation="load",
):
    """对后端执行一次带诊断计量的物理查询。"""

    return measured_query(
        lambda: backend.query(sql),
        emit,
        operation=operation,
        dataset=dataset,
        fields=fields,
        domain=domain,
    )


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
