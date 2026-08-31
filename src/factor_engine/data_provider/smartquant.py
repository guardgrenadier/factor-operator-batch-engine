"""面向批量任务的 SmartQuant 数据提供方门面（日历、资产轴、绑定、加载）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..domain import get_step_values, normalize_date_key, stable_hash
from ..formula import SourceRefExpr
from ..model import (
    DataProviderError,
    DomainError,
    InputSpec,
    NormalizedSourceBatch,
    ReadDomain,
    ReaderRequest,
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
from .datasets import READER_MODES, READER_REGISTRY, reader_compatibility
from .normalize import LoadNormalizer


class SmartQuantDataProvider:
    """直接服务一次 batch 任务的正式 SmartQuant DataProvider。"""

    def __init__(
        self,
        *,
        backend: Any | None = None,
        duckdb: DuckDBBackend | None = None,
        source_config: Mapping[str, Any] | str | Path | None = None,
    ) -> None:
        """初始化后端、诊断列表并加载数据集目录。"""

        self.backend = backend or OceanBaseBackend()
        self.duckdb = duckdb or DuckDBBackend()
        self.diagnostics: list[dict[str, Any]] = []
        self._calendar: np.ndarray | None = None
        self._axes: dict[str, np.ndarray] = {}
        self._axis_requests: dict[str, tuple[Any, ...]] = {}
        self.catalog = Catalog(self.backend, self.duckdb, source_config, self._event)
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
            dataset = self.catalog.asset_datasets[asset_type]
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
                dataset=dataset.params["table"],
                status="ok",
                cache_hit=True,
                physical_queries=0,
            )
            return self._axes[asset_type]
        # 构造日期、交易日标志与显式代码的过滤条件。
        params = dataset.params
        code_col = str(params["code_col"])
        filters = [
            f"{sql_identifier(params['date_col'])} BETWEEN {sql_literal(date_keys[0])} "
            f"AND {sql_literal(date_keys[-1])}"
        ]
        explicit = None if isinstance(selector, str) else tuple(selector)
        if params.get("trading_flag_col"):
            filters.append(f"{sql_identifier(params['trading_flag_col'])} = 1")
        if explicit is not None:
            filters.append(f"{sql_identifier(code_col)} IN ({integer_list(explicit)})")
        # 查询资产轴代码并校验显式选择无缺失，最后冻结结果。
        rows = self._query(
            f"SELECT DISTINCT {sql_identifier(code_col)} AS InnerCode "
            f"FROM {sql_table(params['table'])} "
            f"WHERE {' AND '.join(filters)} ORDER BY {sql_identifier(code_col)}",
            params["table"],
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
        return axis

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
            # Catalog 负责物理落点；Term 原生坐标负责构造当前分区的 Source ReadDomain。
            source_spec, value_kind = self.catalog.bind(term.source_ref)
            assert source_spec.dataset_id is not None
            dataset = self.catalog.datasets[source_spec.dataset_id]
            domain = term.source_domain
            assert domain.codes is not None
            source_domain = ReadDomain(
                read_domain.dates,
                read_domain.write_dates,
                domain.codes,
                tuple(get_step_values(domain.frequency, domain.step_count)),
                read_domain.output_slice,
            )
            # 只把影响物理行集合或坐标解码的参数放入合批身份，字段和值语义不拆组。
            group = stable_hash(
                "load_group",
                dataset.dataset_id,
                dataset.reader,
                dataset.query_builder,
                reader_compatibility(dataset, source_spec),
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

    def load_many(self, bindings: Sequence[SourceBinding]) -> NormalizedSourceBatch:
        """加载单个加载组内的全部绑定，返回 term_id 到数组的映射。"""

        if not bindings:
            return NormalizedSourceBatch({})
        dataset_ids = {binding.source_spec.dataset_id for binding in bindings}
        if len(dataset_ids) != 1 or None in dataset_ids:
            raise DataProviderError("LoadGroup bindings must reference one dataset")
        dataset = self.catalog.datasets[str(next(iter(dataset_ids)))]

        # context 只携带 Reader 的运行依赖，不进入公式或 LogicalPlan 语义身份。
        context: dict[str, Any] = {
            "sql_backend": self.backend,
            "duckdb": self.duckdb,
            "axes": self._axes,
            "emit": self._event,
        }
        if dataset.query_builder == "adjust_factor":
            context["anchor_dataset"] = self.catalog.datasets[
                str(dataset.params["anchor_dataset_id"])
            ]
        if dataset.reader == "parquet_bars":
            code_map = dataset.params.get("code_map", {})
            if code_map.get("mode") == "dated":
                context["code_map_dataset"] = self.catalog.datasets[
                    str(code_map["dataset_id"])
                ]
        request = ReaderRequest(
            dataset,
            tuple(bindings),
            bindings[0].read_domain,
            context,
        )
        try:
            # Reader 流不落地中间结果，直接由唯一 LoadNormalizer 增量消费。
            return LoadNormalizer(
                bindings, READER_MODES[dataset.reader]
            ).normalize(
                READER_REGISTRY[dataset.reader](request)
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
