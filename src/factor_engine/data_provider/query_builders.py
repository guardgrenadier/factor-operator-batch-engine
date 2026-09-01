"""具名 SQL Query Builder：只生成受约束的物理查询，不执行、不散布。

每个 builder 接收 ReaderRequest 与稳定值列别名，返回 SQL 与物理字段诊断信息。
结果必须转换为规范 labels 布局：DataDate、InnerCode、可选 Step、value_0...。
builder 不散布数组、不应用 default、不处理 dtype/Infinity/ValueKind。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Mapping

from ..model import DataProviderError
from .backend import sql_identifier, sql_literal, sql_table, integer_list

if TYPE_CHECKING:
    from .readers import ReaderRequest


# builder 返回值：(SQL, 物理字段诊断信息)。
Built = tuple[str, tuple[str, ...]]


def panel_fields(request: ReaderRequest, aliases: Mapping[str, str]) -> Built:
    """日期 × 资产长表面板：多字段/常量投影、交易标志与可选指数过滤。"""
    first, domain = request.bindings[0].source_spec, request.bindings[0].read_domain
    date_col = str(first.params["date_col"])
    code_col = str(first.params.get("code_col", "InnerCode"))
    # 逐绑定投影：常量列直接投影字面量，普通绑定读取物理字段。
    select: list[str] = []
    fields: list[str] = []
    for binding in request.bindings:
        alias = sql_identifier(aliases[binding.term_id])
        constant = binding.source_spec.params.get("constant")
        if constant is not None:
            select.append(f"{float(constant)} AS {alias}")
            continue
        field = str(binding.source_spec.field or binding.source_spec.name)
        select.append(f"{sql_identifier(field)} AS {alias}")
        fields.append(field)
    # 构造读取域的日期与资产过滤条件。
    filters = [
        f"{sql_identifier(date_col)} BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])}",
        f"{sql_identifier(code_col)} IN ({integer_list(domain.codes)})",
    ]
    trading_col = first.params.get("trading_flag_col")
    if trading_col:
        filters.append(f"{sql_identifier(str(trading_col))} = 1")
    # 指数权重/成员等 selector 过滤只缩小物理行集合，不改变结果布局。
    if "index_inner_code" in first.params:
        filters.append(f"IndexInnerCode = {int(first.params['index_inner_code'])}")
    elif "index_code" in first.params:
        filters.append(f"IndexCode = {sql_literal(first.params['index_code'])}")
    sql = (
        f"SELECT {sql_identifier(date_col)} AS DataDate, "
        f"{sql_identifier(code_col)} AS InnerCode, {', '.join(select)} "
        f"FROM {sql_table(first.table or '')} WHERE {' AND '.join(filters)}"
    )
    return sql, tuple(fields)


def adjust_factor(request: ReaderRequest, aliases: Mapping[str, str]) -> Built:
    """复权因子：基于交易行和生效日的 as-of 相关查询。"""
    if len(request.bindings) != 1:
        raise _error("adjust_factor is one derived field")
    binding = request.bindings[0]
    domain = binding.read_domain
    alias = sql_identifier(aliases[binding.term_id])
    sql = (
        "SELECT s.DataDate, s.InnerCode, COALESCE(("
        "SELECT a.RatioAdjustingFactor FROM JYDB.DZ_AdjustingFactor a "
        "WHERE a.InnerCode = s.InnerCode AND s.TradingDay >= a.ExDiviDate "
        f"ORDER BY a.ExDiviDate DESC LIMIT 1), 1) AS {alias} "
        "FROM SmartQuant.ReturnDaily s "
        f"WHERE s.DataDate BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND s.InnerCode IN ({integer_list(domain.codes)}) AND s.IfTradingDay = 1"
    )
    return sql, ("RatioAdjustingFactor",)


def untradable(request: ReaderRequest, aliases: Mapping[str, str]) -> Built:
    """不可交易：多个具名交易状态列经或运算派生一个 mask 字段。"""
    if len(request.bindings) != 1:
        raise _error("untradable is one derived field")
    binding = request.bindings[0]
    domain = binding.read_domain
    columns = tuple(binding.source_spec.params.get("columns", UNTRADABLE_COLUMNS))
    flags = " OR ".join(
        f"COALESCE({sql_identifier(str(name))}, 0) = 1" for name in columns
    )
    alias = sql_identifier(aliases[binding.term_id])
    sql = (
        "SELECT DataDate, InnerCode, "
        f"CASE WHEN {flags} THEN 1 ELSE 0 END AS {alias} "
        f"FROM {sql_table(binding.source_spec.table or '')} "
        f"WHERE DataDate BETWEEN {sql_literal(domain.dates[0])} "
        f"AND {sql_literal(domain.dates[-1])} "
        f"AND InnerCode IN ({integer_list(domain.codes)})"
    )
    return sql, tuple(str(name) for name in columns)


def _error(message: str) -> Exception:
    """Query Builder 契约错误统一使用 DataProviderError。"""
    return DataProviderError(message)


SQL_QUERY_BUILDERS: dict[str, Callable[..., Built]] = {
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
