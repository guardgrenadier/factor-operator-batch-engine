"""数据集目录（Catalog）：加载配置、描述与绑定逻辑数据源。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..domain import ValueKind, get_freq_step_count, parse_feature_key, stable_hash
from ..formula import SourceRefExpr
from ..model import DataProviderError, InputSpec, SourceSpec
from .backend import DuckDBBackend, column, measured_query, sql_literal


class Catalog:
    """一次任务使用的数据集目录；内部结构刻意保持为普通字典。"""

    def __init__(
        self,
        backend: Any,
        duckdb: DuckDBBackend,
        config: Mapping[str, Any] | str | Path | None,
        emit: Callable[..., None],
    ) -> None:
        """加载配置并构建数据集、逻辑 source 与资产轴数据集索引。"""

        payload = load_config(config)
        if int(payload.get("schema_version", 0)) != 3:
            raise DataProviderError("data_sources.json schema_version must be 3")

        self.backend = backend
        self.duckdb = duckdb
        self.emit = emit
        # datasets[id] 是物理表；sources[逻辑 key] 是这个字段的物理落点。
        # sources 使用列表，只因为基本面允许同名 ItemName 用 data_code 消歧。
        self.datasets: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, list[dict[str, Any]]] = {}
        self.asset_datasets: dict[str, dict[str, Any]] = {}

        for record in payload.get("source_tables", ()):
            dataset = self._add_dataset(record)
            fields = tuple(record.get("fields", ()))
            if not fields:
                try:
                    fields = self._scan_fields(dataset, record)
                except FileNotFoundError as exc:
                    emit(
                        operation="catalog",
                        dataset=dataset["table"],
                        status="unavailable",
                        error=type(exc).__name__,
                        physical_queries=0,
                    )
            ignored = DEFAULT_EXCLUDES | set(record.get("exclude_fields", ()))
            for field in fields:
                name = str(field)
                if name and not name.startswith("_") and name not in ignored:
                    self._add_source(
                        f"{dataset['asset']}.{dataset['frequency']}.{name}",
                        dataset["id"],
                        name,
                        SCANNED_FIELD_KINDS.get(name, "numeric"),
                    )

        self._add_fundamentals()

        for key, record in payload.get("sources", {}).items():
            dataset = self._add_dataset(record)
            self.sources[str(key)] = [
                {
                    "dataset": dataset["id"],
                    "field": str(record["field"]),
                    "kind": str(record.get("value_kind", "numeric")).lower(),
                    "params": dict(record.get("params", {})),
                }
            ]

        self.fingerprint = stable_hash(self.datasets, self.sources)

    @property
    def source_count(self) -> int:
        """目录中逻辑 source 的总数（含同名消歧条目）。"""

        return sum(len(items) for items in self.sources.values())

    def describe(self, ref: SourceRefExpr) -> InputSpec:
        """根据数据源引用解析出供编译使用的语义输入规格。"""

        source, dataset, params = self._resolve(ref)
        return InputSpec(
            dataset["asset"],
            dataset["frequency"],
            int(params.get("quarters", get_freq_step_count(dataset["frequency"]))),
            ValueKind(source["kind"]),
            "cn_a_share",
        )

    def bind(self, ref: SourceRefExpr) -> tuple[SourceSpec, ValueKind]:
        """将数据源引用绑定为物理源规格（落点表、字段与读取参数）。"""

        source, dataset, params = self._resolve(ref)
        key = parse_feature_key(ref.logical_key)
        field = (
            str(params["column_name"])
            if dataset["source"] == "Fundamental"
            else source["field"]
        )
        spec = SourceSpec(
            key.asset,
            key.freq,
            key.name,
            dataset["source"],
            dataset["table"],
            field,
            {
                "dataset_id": dataset["id"],
                "date_col": dataset["date_col"],
                "date_col_type": dataset["date_col_type"],
                "code_col": dataset["code_col"],
                "duckdb_threads": dataset["duckdb_threads"],
                "trading_flag_col": dataset["trading_flag_col"],
                "path_template": dataset["path_template"],
                "data_type": dataset["data_type"],
                **params,
            },
            dataset["reader"],
            dataset["query_builder"],
        )
        return spec, ValueKind(source["kind"])

    def _resolve(self, ref: SourceRefExpr):
        """按逻辑 key 与 data_code 消歧，选出唯一 source、数据集与参数。"""

        candidates = list(self.sources.get(ref.logical_key, ()))
        params = dict(ref.semantic_params)
        if "data_code" in params:
            candidates = [
                source
                for source in candidates
                if int(source["params"].get("data_code", -1))
                == int(params["data_code"])
            ]
        if not candidates:
            raise DataProviderError(f"Unknown source {ref.logical_key!r}")
        if len(candidates) > 1:
            codes = [source["params"].get("data_code") for source in candidates]
            raise DataProviderError(
                f"Source {ref.logical_key!r} is ambiguous; specify data_code from {codes}"
            )
        source = candidates[0]
        return (
            source,
            self.datasets[source["dataset"]],
            {**source["params"], **params},
        )

    def _add_dataset(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """登记一个物理数据集，校验具名 Reader/Query Builder 并推导坐标列名。"""

        source = str(record["source"])
        asset = str(record["asset"])
        frequency = str(record["freq"])
        table = str(record["table"])
        # Reader 与 Query Builder 选择必须显式声明，且只存在于数据集配置。
        reader = record.get("reader")
        if reader not in READER_NAMES:
            raise DataProviderError(
                f"Dataset {table!r} requires a known reader, got {reader!r}"
            )
        query_builder = record.get("query_builder")
        if reader == "sql_reader":
            if query_builder not in QUERY_BUILDER_NAMES:
                raise DataProviderError(
                    f"Dataset {table!r} requires a known query_builder, "
                    f"got {query_builder!r}"
                )
        elif query_builder is not None:
            raise DataProviderError(
                f"Dataset {table!r} declares query_builder but reader "
                f"{reader!r} does not use one"
            )
        dataset_id = str(
            record.get("dataset_id")
            or stable_hash(source, asset, frequency, table)[:16]
        )
        dataset = {
            "id": dataset_id,
            "asset": asset,
            "frequency": frequency,
            "source": source,
            "table": table,
            "reader": str(reader),
            "query_builder": (
                str(query_builder) if query_builder is not None else None
            ),
            "date_col": str(
                record.get("date_col")
                or ("TradingDay" if source == "IndexQuote" else "DataDate")
            ),
            "code_col": str(record.get("code_col") or "InnerCode"),
            "trading_flag_col": record.get("trading_flag_col"),
            "path_template": record.get("path_template"),
            "data_type": record.get("data_type"),
            "date_col_type": str(record.get("date_col_type", "date")),
            "duckdb_threads": int(record.get("duckdb_threads", 8)),
        }
        self.datasets.setdefault(dataset_id, dataset)
        if record.get("asset_axis"):
            self.asset_datasets[asset] = dataset
        return dataset

    def _add_source(
        self,
        key: str,
        dataset: str,
        field: str,
        kind: str = "numeric",
        params: Mapping[str, Any] | None = None,
        *,
        duplicate: bool = False,
    ) -> None:
        """在指定逻辑 key 下追加一个 source 落点，除非允许否则拒绝重复。"""

        items = self.sources.setdefault(key, [])
        if items and not duplicate:
            raise DataProviderError(f"Duplicate catalog source {key!r}")
        items.append(
            {
                "dataset": dataset,
                "field": field,
                "kind": kind,
                "params": dict(params or {}),
            }
        )

    def _scan_fields(
        self, dataset: Mapping[str, Any], record: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """扫描物理数据集的真实字段清单（分钟 parquet 或 SQL 列）。"""

        if dataset["reader"] == "parquet_bars":
            sample = minute_path(dataset, record.get("sample_date", "2024-12-31"))
            if not sample.exists():
                raise FileNotFoundError(sample)
            return self.duckdb.parquet_fields(sample)
        schema, table = dataset["table"].split(".", 1)
        sql = (
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = {sql_literal(schema)} "
            f"AND TABLE_NAME = {sql_literal(table)} ORDER BY ORDINAL_POSITION"
        )
        rows = measured_query(
            lambda: self.backend.query(sql),
            self.emit,
            operation="catalog",
            dataset=dataset["table"],
        )
        return tuple(str(value) for value in rows[column(rows, "COLUMN_NAME")])

    def _add_fundamentals(self) -> None:
        """查询基本面 Item 清单，为每个 Item 注册数据集与逻辑 source。"""

        sql = (
            "SELECT c.ItemCode, c.ItemName "
            "FROM SmartQuant.Fundamental_ItemCode c "
            "INNER JOIN information_schema.TABLES t "
            "ON t.TABLE_SCHEMA = 'SmartQuant' "
            "AND t.TABLE_NAME = CONCAT('Fundamental_Item', c.ItemCode) "
            "ORDER BY c.ItemCode"
        )
        rows = measured_query(
            lambda: self.backend.query(sql),
            self.emit,
            operation="catalog",
            dataset="SmartQuant.Fundamental_ItemCode",
        )
        if rows.empty:
            return
        code_col, name_col = column(rows, "ItemCode"), column(rows, "ItemName")
        for code, name in zip(rows[code_col], rows[name_col], strict=True):
            if name is None:
                continue
            dataset = self._add_dataset(
                {
                    "asset": "stk",
                    "freq": "1d",
                    "source": "Fundamental",
                    "table": f"SmartQuant.Fundamental_Item{int(code)}",
                    "dataset_id": f"fundamental:{int(code)}",
                    "reader": "fundamental",
                }
            )
            self._add_source(
                f"stk.1d.{name}",
                dataset["id"],
                "CumLatest",
                params={
                    "data_code": int(code),
                    "column_name": "CumLatest",
                    "quarters": 1,
                    "publ_date_limit": -180,
                },
                duplicate=True,
            )


def load_config(config: Mapping[str, Any] | str | Path | None) -> dict[str, Any]:
    """读取 data_sources.json（可传字典、路径或默认内置文件）为配置字典。"""

    if isinstance(config, Mapping):
        return dict(config)
    path = (
        Path(config)
        if config is not None
        else Path(__file__).with_name("data_sources.json")
    )
    return json.loads(path.read_text(encoding="utf-8"))


def minute_path(dataset: Mapping[str, Any], date: Any) -> Path:
    """按 path_template 与日期、data_type 渲染出单个分钟 parquet 文件路径。"""

    template = dataset.get("path_template")
    if not template:
        raise DataProviderError(
            f"Minute dataset {dataset['id']!r} has no path_template"
        )
    date_key = str(date).replace("-", "")[:8]
    return Path(
        str(template).format(date=date_key, data_type=dataset.get("data_type") or "")
    )


READER_NAMES = frozenset(
    {"sql_reader", "fundamental", "parquet_bars", "cb_stock_map"}
)

QUERY_BUILDER_NAMES = frozenset({"panel_fields", "adjust_factor", "untradable"})

DEFAULT_EXCLUDES = {
    "DataDate",
    "ID",
    "InnerCode",
    "IndustryName",
    "IndustryNameNew",
    "JSID",
    "SecuCode",
    "SecuAbbr",
    "IfTradingDay",
    "TradingDay",
    "UpdateTime",
    "XGRQ",
    "security_code",
    "trading_day",
    "start_time",
    "filename",
}

SCANNED_FIELD_KINDS = {
    "CompanyCode": "code",
    "IndustryCode": "code",
    "SecondIndustryCode": "code",
    "IndustryCodeNew": "code",
    "SecondIndustryCodeNew": "code",
    "ListedSector": "code",
    "ListedStatus": "code",
    "SecuMarket": "code",
    "IfWeekEnd": "mask",
    "IfMonthEnd": "mask",
    "IfQuarterEnd": "mask",
    "IfYearEnd": "mask",
    "IfSpecialTrade": "mask",
    "IfSuspended": "mask",
}
