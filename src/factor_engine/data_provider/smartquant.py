"""面向批量任务的 SmartQuant 数据提供方门面（日历、资产轴、绑定、加载）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..domain import get_step_values, normalize_date_key, stable_hash
from ..formula import SourceRefExpr
from ..model import (
    DataProviderError,
    DomainError,
    InputSpec,
    ReadDomain,
    SourceBinding,
    SourceTerm,
)
from .backend import (
    DuckDBBackend,
    OceanBaseBackend,
    column,
    integer_list,
    measured_query,
    sql_identifier,
    sql_literal,
    sql_table,
)
from .catalog import Catalog
from .readers import load_group, query_code_map


# 任务资产轴与 InnerCode↔SecuCode 映射是本环境的固定物理事实，按资产
# 显式登记在此；它们不属于可挂载数据源配置，也不随 include_tables 过滤。
# 未登记的资产类型在 asset_codes/代码映射冻结时直接报错。
_ASSET_AXES: dict[str, dict[str, Any]] = {
    "stk": {
        "table": "SmartQuant.ReturnDaily",
        "date_col": "DataDate",
        "code_col": "InnerCode",
        "trading_flag_col": "IfTradingDay",
    },
    "cb": {
        "table": "SmartQuant.CBReturnDaily",
        "date_col": "DataDate",
        "code_col": "InnerCode",
        "trading_flag_col": "IfTradingDay",
    },
    "idx": {
        "table": "JYDB.QT_IndexQuote",
        "date_col": "TradingDay",
        "code_col": "InnerCode",
        "trading_flag_col": None,
    },
}

_CODE_MAPS: dict[str, dict[str, Any]] = {
    "stk": {"table": "SmartQuant.InnerCode_SecuCode"},
    "cb": {"from_asset_axis": True},
}


class SmartQuantDataProvider:
    """直接服务一次 batch 任务的正式 SmartQuant DataProvider。"""

    def __init__(
        self,
        *,
        backend: Any | None = None,
        duckdb: DuckDBBackend | None = None,
        source_config: Mapping[str, Any] | str | Path | None = None,
        include_tables: Sequence[str] | None = None,
    ) -> None:
        """初始化后端、诊断列表并加载数据集目录。

        include_tables 非空时启用白名单：source_tables 条目按 name 过滤，
        sources 段按逻辑键过滤，基本面自动注册需显式包含伪名字
        "fundamentals"（例如只挂 smartquant_stk_daily/smartquant_cb_daily
        日行情、不挂分钟 parquet）。
        """

        self.backend = backend or OceanBaseBackend()
        self.duckdb = duckdb or DuckDBBackend()
        self.diagnostics: list[dict[str, Any]] = []
        self._calendar: np.ndarray | None = None
        self._axes: dict[str, np.ndarray] = {}
        self._axis_requests: dict[str, tuple[Any, ...]] = {}
        self._code_maps: dict[str, pd.DataFrame] = {}
        self.catalog = Catalog(
            self.backend,
            self.duckdb,
            source_config,
            self._event,
            include_tables,
        )
        self.catalog_fingerprint = self.catalog.fingerprint
        self._event(
            operation="catalog_snapshot",
            dataset="data_sources.json",
            status="ok",
            fingerprint=self.catalog_fingerprint,
            sources=self.catalog.source_count,
            physical_queries=0,
        )

    def calendar_dates(self, calendar: str) -> np.ndarray:
        """返回并缓存指定日历的全部交易日数组。"""

        if calendar not in {"default", "cn_a_share"}:
            raise DomainError(f"Unknown calendar {calendar!r}")
        if self._calendar is None:
            rows = self._query(
                "SELECT TradingDate FROM SmartQuant.JY_TradingDayNew "
                "WHERE SecuMarket = 83 AND IfTradingDay = 1 ORDER BY TradingDate",
                "SmartQuant.JY_TradingDayNew",
                "calendar",
            )
            self._calendar = np.asarray(
                [
                    normalize_date_key(value)
                    for value in rows[column(rows, "TradingDate")].tolist()
                ]
            )
        else:
            self._event(
                operation="calendar",
                dataset="SmartQuant.JY_TradingDayNew",
                status="ok",
                cache_hit=True,
                physical_queries=0,
            )
        return self._calendar

    def asset_codes(
        self,
        asset_type: str,
        dates: Sequence[Any] | None = None,
        selector: str | Sequence[Any] = "all",
    ) -> np.ndarray:
        """解析并冻结某资产类型的有序任务资产范围（资产代码轴）。"""

        try:
            axis_spec = _ASSET_AXES[asset_type]
        except KeyError as exc:
            raise DomainError(f"Unknown asset type {asset_type!r}") from exc
        if not dates:
            raise DomainError("Task asset axis requires read-horizon dates")
        date_keys = tuple(normalize_date_key(value) for value in dates)
        selection = selector if isinstance(selector, str) else tuple(selector)
        request = (date_keys, selection)
        if asset_type in self._axis_requests:
            if self._axis_requests[asset_type] != request:
                raise DomainError(f"Asset axis {asset_type!r} is already frozen")
            self._event(
                operation="asset_axis",
                dataset=axis_spec["table"],
                status="ok",
                cache_hit=True,
                physical_queries=0,
            )
            return self._axes[asset_type]
        # 构造日期、交易日标志与显式代码的过滤条件。
        code_col = str(axis_spec["code_col"])
        filters = [
            f"{sql_identifier(axis_spec['date_col'])} BETWEEN {sql_literal(date_keys[0])} "
            f"AND {sql_literal(date_keys[-1])}"
        ]
        explicit = None if isinstance(selector, str) else tuple(selector)
        if axis_spec["trading_flag_col"]:
            filters.append(f"{sql_identifier(axis_spec['trading_flag_col'])} = 1")
        if explicit is not None:
            filters.append(f"{sql_identifier(code_col)} IN ({integer_list(explicit)})")
        # 查询资产轴代码并校验显式选择无缺失，最后冻结结果。
        rows = self._query(
            f"SELECT DISTINCT {sql_identifier(code_col)} AS InnerCode "
            f"FROM {sql_table(axis_spec['table'])} "
            f"WHERE {' AND '.join(filters)} ORDER BY {sql_identifier(code_col)}",
            axis_spec["table"],
            "asset_axis",
        )
        found = [int(value) for value in rows[column(rows, "InnerCode")].tolist()]
        if explicit is not None:
            missing = [value for value in explicit if int(value) not in set(found)]
            if missing:
                raise DomainError(
                    f"Asset codes absent from {asset_type!r} task domain: {missing}"
                )
            found = [int(value) for value in explicit]
        axis = np.asarray(found, dtype=np.int64)
        if len(axis) == 0:
            raise DomainError(f"Asset axis {asset_type!r} is empty")
        self._axis_requests[asset_type] = request
        self._axes[asset_type] = axis
        self._freeze_code_map(asset_type)
        return axis

    def _freeze_code_map(self, asset_type: str) -> None:
        """在资产轴冻结时一并冻结该资产的 InnerCode↔SecuCode 映射。

        只当该资产有已挂载的 secu_code 数据源时才发起查询；映射来源
        由代码注册表显式登记，未登记的资产直接报错。映射在整个任务
        期间保持不变，Reader 从冻结上下文取用而不再逐次查询。
        """
        if asset_type in self._code_maps:
            return
        if not self.catalog.needs_code_map(asset_type):
            return
        try:
            map_spec = _CODE_MAPS[asset_type]
        except KeyError as exc:
            raise DataProviderError(
                f"Asset {asset_type!r} has secu_code datasets but no "
                "registered code map"
            ) from exc
        date_keys, _ = self._axis_requests[asset_type]
        self._code_maps[asset_type] = query_code_map(
            self.backend,
            map_spec,
            _ASSET_AXES[asset_type],
            date_keys,
            self._axes[asset_type].tolist(),
            self._event,
        )

    def describe_many(
        self, source_refs: Sequence[SourceRefExpr]
    ) -> Mapping[SourceRefExpr, InputSpec]:
        """批量将数据源引用解析为输入规格。"""

        return {ref: self.catalog.describe(ref) for ref in source_refs}

    def bind_many(
        self, source_terms: Sequence[SourceTerm], read_domain: ReadDomain
    ) -> Sequence[SourceBinding]:
        """把每个数据源节点绑定为物理源规格、读取域并归入加载组。"""

        bindings: list[SourceBinding] = []
        for term in source_terms:
            source_spec, value_kind = self.catalog.bind(term.source_ref)
            assert term.domain is not None and term.domain.codes is not None
            source_domain = ReadDomain(
                read_domain.dates,
                read_domain.write_dates,
                term.domain.codes,
                tuple(get_step_values(term.domain.frequency, term.domain.step_count)),
                read_domain.output_slice,
            )
            # 加载组兼容键：数据集、Reader、Query Builder 与影响物理行集合或
            # 坐标解码的共享参数；字段级参数（字段、常量、默认值、投影）不拆组。
            compatibility = dict(source_spec.params)
            for field_level in _FIELD_LEVEL_PARAMS:
                compatibility.pop(field_level, None)
            group = stable_hash(
                "load_group",
                source_spec.reader,
                source_spec.query_builder,
                source_spec.table,
                compatibility,
                source_domain.dates,
                source_domain.codes,
                source_domain.steps,
            )
            bindings.append(
                SourceBinding(
                    term.term_id,
                    source_spec,
                    source_domain,
                    group,
                    value_kind,
                )
            )
        return bindings

    def load_many(self, bindings: Sequence[SourceBinding]) -> Mapping[str, np.ndarray]:
        """加载单个加载组内的全部绑定，返回 term_id 到数组的映射。"""

        if not bindings:
            return {}
        group = bindings[0].load_group_key
        if any(binding.load_group_key != group for binding in bindings[1:]):
            raise DataProviderError("load_many requires one LoadGroup")
        try:
            return load_group(
                bindings,
                self.catalog,
                self.backend,
                self.duckdb,
                self._axes,
                self._code_maps,
                self._event,
            )
        except DataProviderError:
            raise
        except Exception as exc:
            keys = [binding.source_spec.key for binding in bindings]
            raise DataProviderError(f"LoadGroup failed for {keys}") from exc

    def _query(self, sql: str, dataset: str, operation: str):
        """执行一次带诊断计量的物理查询。"""

        return measured_query(
            lambda: self.backend.query(sql),
            self._event,
            operation=operation,
            dataset=dataset,
        )

    def _event(self, **event: Any) -> None:
        """补全默认字段并把一条诊断事件追加到诊断列表。"""

        payload = {"cache_hit": False, "physical_queries": 1, **event}
        payload.setdefault(
            "mode", "batch" if len(payload.get("fields", ())) > 1 else "single"
        )
        self.diagnostics.append(payload)


_FIELD_LEVEL_PARAMS = frozenset(
    {"column_name", "kind", "projection", "constant", "default", "field"}
)
