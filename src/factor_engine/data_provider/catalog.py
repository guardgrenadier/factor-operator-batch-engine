"""数据集目录：发现字段并把逻辑 Source 绑定到 DatasetSpec。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..domain import ValueKind, get_freq_step_count, parse_feature_key, stable_hash
from ..formula import SourceRefExpr
from ..model import DataProviderError, DatasetSpec, InputSpec, SourceSpec
from .backend import DuckDBBackend, column, measured_query, sql_literal
from .datasets import READER_REGISTRY, SQL_QUERY_BUILDERS, minute_path


class Catalog:
    """一次任务使用的 DatasetSpec、逻辑 Source 和资产轴目录。"""

    def __init__(
        self,
        backend: Any,
        duckdb: DuckDBBackend,
        config: Mapping[str, Any] | str | Path | None,
        emit: Callable[..., None],
    ) -> None:
        """加载配置，并冻结本次 Provider 使用的数据集与逻辑 Source 目录。"""

        payload = load_config(config)
        if int(payload.get("schema_version", 0)) != 3:
            raise DataProviderError("data_sources.json schema_version must be 3")

        self.backend = backend
        self.duckdb = duckdb
        self.emit = emit
        self.datasets: dict[str, DatasetSpec] = {}
        self.sources: dict[str, list[dict[str, Any]]] = {}
        self.asset_datasets: dict[str, DatasetSpec] = {}

        # 先完整登记 Dataset，再校验跨 Dataset 依赖，避免配置顺序影响解析结果。
        records = tuple(payload.get("datasets", ()))
        for record in records:
            self._add_dataset(record)
        for dataset in self.datasets.values():
            self._validate_dataset(dataset)

        # 字段发现只扩展 Catalog；发现使用的元数据不会传入 Reader 参数。
        for record in records:
            dataset = self.datasets[str(record["dataset_id"])]
            discover = record.get(
                "discover_fields",
                dataset.reader == "parquet_bars"
                or dataset.query_builder == "panel_fields",
            )
            if not discover:
                continue
            fields = tuple(record.get("fields", ()))
            if not fields:
                try:
                    fields = self._scan_fields(dataset, record)
                except FileNotFoundError as exc:
                    emit(
                        operation="catalog",
                        dataset=str(dataset.params.get("table", dataset.dataset_id)),
                        status="unavailable",
                        error=type(exc).__name__,
                        physical_queries=0,
                    )
            ignored = DEFAULT_EXCLUDES | set(record.get("exclude_fields", ()))
            ignored.update(
                str(record[name])
                for name in (
                    "date_col",
                    "code_col",
                    "trading_flag_col",
                )
                if record.get(name)
            )
            for field in fields:
                name = str(field)
                if name and not name.startswith("_") and name not in ignored:
                    self._add_source(
                        f"{dataset.asset}.{dataset.frequency}.{name}",
                        dataset.dataset_id,
                        field=name,
                        kind=SCANNED_FIELD_KINDS.get(name, "numeric"),
                    )

        # Fundamental ItemCode 使用专属 expander，显式 Source 最后注册并可覆盖扫描结果。
        self._add_fundamentals()
        for key, record in payload.get("sources", {}).items():
            dataset_id = str(record["dataset_id"])
            if dataset_id not in self.datasets:
                raise DataProviderError(
                    f"Source {key!r} references unknown dataset {dataset_id!r}"
                )
            parsed = parse_feature_key(str(key))
            dataset = self.datasets[dataset_id]
            if (parsed.asset, parsed.freq) != (dataset.asset, dataset.frequency):
                raise DataProviderError(
                    f"Source {key!r} does not match dataset {dataset_id!r} domain"
                )
            has_field, has_constant = "field" in record, "constant" in record
            if dataset.query_builder == "panel_fields" and has_field == has_constant:
                raise DataProviderError(
                    f"SQL panel source {key!r} requires exactly one field or constant"
                )
            if dataset.reader in {"parquet_bars", "fundamental"} and not has_field:
                raise DataProviderError(
                    f"Source {key!r} requires one physical field"
                )
            if dataset.reader == "cb_stock_map" and record.get("projection") not in {
                "inner_code",
                "axis_position",
            }:
                raise DataProviderError(
                    f"CB stock map source {key!r} has invalid projection"
                )
            self.sources[str(key)] = [
                {
                    "dataset": dataset_id,
                    "field": record.get("field"),
                    "constant": record.get("constant"),
                    "projection": record.get("projection"),
                    "default": record.get("default", float("nan")),
                    "kind": str(record.get("value_kind", "numeric")).lower(),
                    "params": dict(record.get("params", {})),
                }
            ]

        self.fingerprint = stable_hash(self.datasets, self.sources)

    @property
    def source_count(self) -> int:
        """返回逻辑 Source 条目数，包含需要 data_code 消歧的同名项。"""

        return sum(len(items) for items in self.sources.values())

    def describe(self, ref: SourceRefExpr) -> InputSpec:
        """把逻辑 Source 引用描述为不含物理读取信息的编译期输入规格。"""

        source, dataset, params = self._resolve(ref)
        return InputSpec(
            dataset.asset,
            dataset.frequency,
            int(params.get("quarters", get_freq_step_count(dataset.frequency))),
            ValueKind(source["kind"]),
            "cn_a_share",
        )

    def bind(self, ref: SourceRefExpr) -> tuple[SourceSpec, ValueKind]:
        """把逻辑 Source 引用绑定到 Dataset、字段和 Reader 所需参数。"""

        source, dataset, params = self._resolve(ref)
        key = parse_feature_key(ref.logical_key)
        field = params.get("column_name", source.get("field"))
        spec = SourceSpec(
            key.asset,
            key.freq,
            key.name,
            source=dataset.reader,
            table=dataset.params.get("table"),
            field=None if field is None else str(field),
            params=params,
            dataset_id=dataset.dataset_id,
            constant=source.get("constant"),
            default=source.get("default", float("nan")),
            projection=source.get("projection"),
        )
        return spec, ValueKind(source["kind"])

    def _resolve(
        self, ref: SourceRefExpr
    ) -> tuple[dict[str, Any], DatasetSpec, dict[str, Any]]:
        """按逻辑 key 和可选 data_code 解析唯一 Source、Dataset 与合并参数。"""

        candidates = list(self.sources.get(ref.logical_key, ()))
        semantic_params = dict(ref.semantic_params)
        if "data_code" in semantic_params:
            candidates = [
                source
                for source in candidates
                if int(source["params"].get("data_code", -1))
                == int(semantic_params["data_code"])
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
            {**source["params"], **semantic_params},
        )

    def _add_dataset(self, record: Mapping[str, Any]) -> DatasetSpec:
        """从配置登记 DatasetSpec，并记录可提供任务资产轴的数据集。"""

        try:
            dataset_id = str(record["dataset_id"])
            reader = str(record["reader"])
            asset = str(record["asset"])
            frequency = str(record["freq"])
            query_builder = record.get("query_builder")
        except KeyError as exc:
            raise DataProviderError(f"Dataset is missing {exc.args[0]!r}") from exc
        if reader not in READER_REGISTRY:
            raise DataProviderError(f"Unknown reader {reader!r} for dataset {dataset_id!r}")
        # Catalog 元数据不属于物理读取契约，不放入 DatasetSpec.params。
        params = {
            str(key): value
            for key, value in record.items()
            if key
            not in {
                "dataset_id",
                "reader",
                "query_builder",
                "asset",
                "freq",
                "asset_axis",
                "discover_fields",
                "fields",
                "exclude_fields",
                "sample_date",
            }
        }
        dataset = DatasetSpec(
            dataset_id,
            reader,
            asset,
            frequency,
            params,
            None if query_builder is None else str(query_builder),
        )
        existing = self.datasets.get(dataset_id)
        if existing is not None and existing != dataset:
            raise DataProviderError(f"Duplicate dataset_id {dataset_id!r}")
        self.datasets[dataset_id] = dataset
        if record.get("asset_axis"):
            self.asset_datasets[asset] = dataset
        return dataset

    def _validate_dataset(self, dataset: DatasetSpec) -> None:
        """在 Catalog 冻结前校验 Reader 最小配置和 Dataset 依赖。"""

        params = dataset.params
        required = {
            "sql_reader": (),
            "parquet_bars": (
                "path_template",
                "code_map",
            ),
            "fundamental": (
                "table",
                "date_col",
                "code_col",
                "rank_col",
                "report_date_col",
                "publication_date_col",
            ),
            "cb_stock_map": ("bond_code_table", "relation_table"),
        }[dataset.reader]
        if dataset.reader != "sql_reader" and dataset.query_builder is not None:
            raise DataProviderError(
                f"Dataset {dataset.dataset_id!r} cannot set query_builder for "
                f"reader {dataset.reader!r}"
            )
        if dataset.reader == "sql_reader":
            if dataset.query_builder not in SQL_QUERY_BUILDERS:
                raise DataProviderError(
                    f"Dataset {dataset.dataset_id!r} has unknown query_builder "
                    f"{dataset.query_builder!r}"
                )
            required = {
                "panel_fields": ("table", "date_col", "code_col"),
                "adjust_factor": ("anchor_dataset_id", "factor_table"),
                "untradable": ("table", "date_col", "code_col"),
            }[dataset.query_builder]
        missing = [name for name in required if name not in params]
        if missing:
            raise DataProviderError(
                f"Dataset {dataset.dataset_id!r} is missing config {missing}"
            )
        if dataset.query_builder == "adjust_factor":
            dependency = str(params["anchor_dataset_id"])
            if dependency not in self.datasets:
                raise DataProviderError(
                    f"Dataset {dataset.dataset_id!r} references unknown anchor {dependency!r}"
                )
        if dataset.reader == "parquet_bars":
            code_map = params["code_map"]
            mode = code_map.get("mode")
            if mode == "static" and "table" not in code_map:
                raise DataProviderError(
                    f"Dataset {dataset.dataset_id!r} static code_map requires table"
                )
            if mode == "dated":
                dependency = str(code_map.get("dataset_id", ""))
                if dependency not in self.datasets:
                    raise DataProviderError(
                        f"Dataset {dataset.dataset_id!r} references unknown code_map dataset {dependency!r}"
                    )
            if mode not in {"static", "dated"}:
                raise DataProviderError(
                    f"Dataset {dataset.dataset_id!r} has invalid code_map mode {mode!r}"
                )

    def _add_source(
        self,
        key: str,
        dataset: str,
        *,
        field: str | None = None,
        constant: Any | None = None,
        projection: str | None = None,
        default: Any = float("nan"),
        kind: str = "numeric",
        params: Mapping[str, Any] | None = None,
        duplicate: bool = False,
    ) -> None:
        """登记一个逻辑 Source 落点，并按需允许 Fundamental 同名项。"""

        items = self.sources.setdefault(key, [])
        if items and not duplicate:
            raise DataProviderError(f"Duplicate catalog source {key!r}")
        items.append(
            {
                "dataset": dataset,
                "field": field,
                "constant": constant,
                "projection": projection,
                "default": default,
                "kind": kind,
                "params": dict(params or {}),
            }
        )

    def _scan_fields(
        self, dataset: DatasetSpec, record: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """从样本 parquet 或 SQL information_schema 发现数据集字段。"""

        if dataset.reader == "parquet_bars":
            sample = minute_path(dataset, record.get("sample_date", "2024-12-31"))
            if not sample.exists():
                raise FileNotFoundError(sample)
            return self.duckdb.parquet_fields(sample)
        table_name = str(dataset.params["table"])
        schema, table = table_name.split(".", 1)
        sql = (
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = {sql_literal(schema)} "
            f"AND TABLE_NAME = {sql_literal(table)} ORDER BY ORDINAL_POSITION"
        )
        rows = measured_query(
            lambda: self.backend.query(sql),
            self.emit,
            operation="catalog",
            dataset=table_name,
        )
        return tuple(str(value) for value in rows[column(rows, "COLUMN_NAME")])

    def _add_fundamentals(self) -> None:
        """发现 Fundamental ItemCode，并扩展对应 Dataset 与逻辑 Source。"""

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
            data_code = int(code)
            dataset = self._add_dataset(
                {
                    "dataset_id": f"fundamental:{data_code}",
                    "reader": "fundamental",
                    "asset": "stk",
                    "freq": "1d",
                    "table": f"SmartQuant.Fundamental_Item{data_code}",
                    "date_col": "DataDate",
                    "code_col": "InnerCode",
                    "rank_col": "EndDateRank",
                    "report_date_col": "EndDate",
                    "publication_date_col": "InfoPublDate",
                }
            )
            self._add_source(
                f"stk.1d.{name}",
                dataset.dataset_id,
                field="CumLatest",
                params={
                    "data_code": data_code,
                    "column_name": "CumLatest",
                    "quarters": 1,
                    "publ_date_limit": -180,
                },
                duplicate=True,
            )


def load_config(config: Mapping[str, Any] | str | Path | None) -> dict[str, Any]:
    """读取调用方配置、指定 JSON 文件或包内默认数据源配置。"""

    if isinstance(config, Mapping):
        return dict(config)
    path = (
        Path(config)
        if config is not None
        else Path(__file__).with_name("data_sources.json")
    )
    return json.loads(path.read_text(encoding="utf-8"))


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


__all__ = ["Catalog"]
