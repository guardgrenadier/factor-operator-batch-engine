"""旧版实现：解析数据源键并按需读取外部输入的路由层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .alignment import feature_array
from .model import (
    ExecutionScope,
    FeatureArray,
    SourceSpec,
    parse_feature_key,
    scope_signature,
    stable_hash,
)
from .smartquant import SmartQuantSourceReader
from .sources import (
    build_data_dict,
    load_source_config,
    lookup_source_record,
    source_spec_from_data_dict,
    source_spec_from_record,
)
from .store import FeatureStore


class DataRouter:
    """解析完整数据源键并直接读取数据而不落盘原始分区。"""

    def __init__(
        self,
        *,
        source_config: dict[str, Any] | str | Path | None = None,
        reader: SmartQuantSourceReader | None = None,
        memory_data: dict[str, np.ndarray | FeatureArray] | None = None,
    ):
        """初始化数据源配置、实时字段目录、Reader 和会话级 source 缓存。"""
        # 配置生成可搜索目录，内存数据键同时规范化。
        self.source_config = load_source_config(source_config)
        self.reader = reader or SmartQuantSourceReader()
        self.data_dict = build_data_dict(self.source_config, reader=self.reader)
        self.memory_data = {
            parse_feature_key(key).key: value
            for key, value in (memory_data or {}).items()
        }
        self.source_overrides: dict[str, SourceSpec] = {}
        self._cache: dict[str, FeatureArray] = {}

    def register_source(self, spec: SourceSpec) -> SourceSpec:
        """注册显式 SourceSpec，并清除该 source 的旧缓存。"""
        # 复制可变参数，避免调用方后续修改已注册规格。
        key = parse_feature_key(spec.key).key
        normalized = SourceSpec(
            asset=spec.asset,
            freq=spec.freq,
            name=spec.name,
            source=spec.source,
            table=spec.table,
            field=spec.field,
            params=dict(spec.params),
        )
        self.source_overrides[key] = normalized
        # 只失效同一逻辑数据源的缓存，其余读取结果继续复用。
        self._cache = {
            cache_key: value
            for cache_key, value in self._cache.items()
            if value.key != key
        }
        return normalized

    def resolve_source(self, raw_key: str) -> SourceSpec:
        """按固定优先级把完整特征键解析为唯一 SourceSpec。"""
        # 内存注入和显式覆盖优先于静态配置与动态字段目录。
        key = parse_feature_key(raw_key).key
        if key in self.memory_data:
            fk = parse_feature_key(key)
            return SourceSpec(
                asset=fk.asset,
                freq=fk.freq,
                name=fk.name,
                source="memory",
                field=fk.name,
            )
        if key in self.source_overrides:
            return self.source_overrides[key]
        # 先查显式 source 记录，再从扫描得到的数据字典推导表字段。
        record = lookup_source_record(self.source_config, key)
        if record is not None:
            return source_spec_from_record(key, record)
        table_spec = source_spec_from_data_dict(
            self.data_dict,
            self.source_config.get("source_tables", []) or [],
            key,
        )
        if table_spec is not None:
            return table_spec
        raise KeyError(f"Source {key!r} is not registered in data_dict")

    def can_resolve(self, raw_key: str) -> bool:
        """判断完整特征键能否解析为已配置 source。"""
        try:
            self.resolve_source(raw_key)
            return True
        except (KeyError, ValueError):
            return False

    def read(
        self,
        raw_key: str,
        store: FeatureStore,
        *,
        dates: Any | None = None,
        scope: ExecutionScope | None = None,
        use_cache: bool = True,
    ) -> FeatureArray:
        """读取临时 source，并按 SourceSpec 和 Snapshot 签名缓存结果。"""
        spec = self.resolve_source(raw_key)
        return self.read_spec(
            spec, store, dates=dates, scope=scope, use_cache=use_cache
        )

    def read_spec(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        dates: Any | None = None,
        scope: ExecutionScope | None = None,
        use_cache: bool = True,
    ) -> FeatureArray:
        """按显式 SourceSpec 读取输入，并按 Snapshot 和 scope 缓存。"""
        # dates 简写先统一为完整执行范围对象。
        if dates is not None:
            if scope is not None:
                raise ValueError("Pass either dates or scope, not both")
            scope = ExecutionScope(read_dates=dates, write_dates=dates)
        # 缓存身份同时包含读取参数、快照版本和日期范围。
        cache_key = stable_hash(
            spec.to_dict(), store.snapshot_signature, scope_signature(scope)
        )
        if use_cache and cache_key in self._cache:
            return self._cache[cache_key]
        # 根据来源分派到特征存储、内存数组或外部 Reader。
        if spec.source == "store":
            feature = store.load_feature(spec.key, scope=scope)
        elif spec.source == "memory":
            feature = self._read_memory(spec, store, scope=scope)
        else:
            feature = self.reader.read_source(spec, store, scope=scope)
        if use_cache:
            self._cache[cache_key] = feature
        return feature

    def load_many(
        self,
        specs: list[SourceSpec] | tuple[SourceSpec, ...],
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
        use_cache: bool = True,
    ) -> dict[str, FeatureArray]:
        """批量读取一个数据源组，当前按单字段路径逐项回退。"""
        # 组内键必须唯一，当前实现复用单项读取及其缓存。
        loaded: dict[str, FeatureArray] = {}
        for spec in specs:
            if spec.key in loaded:
                raise ValueError(f"Duplicate SourceSpec key {spec.key!r} in load group")
            loaded[spec.key] = self.read_spec(
                spec, store, scope=scope, use_cache=use_cache
            )
        return loaded

    def clear_cache(self) -> None:
        """清空当前 Router 会话内的 source 缓存。"""
        self._cache.clear()

    def search(self, name: str) -> pd.DataFrame:
        """按 key、字段名或中文名搜索实时字段目录。"""
        # 临时拼出完整键，并对三个可见名称列执行不区分大小写匹配。
        df = self.data_dict.copy()
        df.insert(
            0,
            "key",
            df["asset"].astype(str)
            + "."
            + df["freq"].astype(str)
            + "."
            + df["field"].astype(str),
        )
        query = str(name)
        mask = (
            df["key"].astype(str).str.contains(query, case=False, na=False, regex=False)
            | df["field"]
            .astype(str)
            .str.contains(query, case=False, na=False, regex=False)
            | df["name_cn"]
            .astype(str)
            .str.contains(query, case=False, na=False, regex=False)
        )
        return df.loc[
            mask, ["key", "asset", "freq", "field", "name_cn", "table"]
        ].reset_index(drop=True)

    def _read_memory(
        self,
        spec: SourceSpec,
        store: FeatureStore,
        *,
        scope: ExecutionScope | None = None,
    ) -> FeatureArray:
        """将内存 source 转换为统一的 FeatureArray。"""
        # 已封装对象仅在请求子范围时按日期裁剪。
        value = self.memory_data[spec.key]
        if isinstance(value, FeatureArray):
            if scope is None:
                return value
            date_pos = {str(date): i for i, date in enumerate(value.space.dates)}
            idx = [date_pos[date] for date in scope.read_dates]
            return FeatureArray(
                key=value.key,
                values=value.values[idx],
                space=store.resolve_space(spec, scope=scope),
                feature_def=value.feature_def,
                missing_value=value.missing_value,
                metadata=dict(value.metadata),
            )
        # 裸数组按执行范围裁剪后交给公共封装路径补齐元数据。
        arr = np.asarray(value)
        if scope is not None:
            if arr.ndim == 2:
                arr = arr[:, :, None]
            target_space = store.resolve_space(spec, scope=scope)
            if arr.shape[0] != target_space.n_dates:
                date_pos = {str(date): i for i, date in enumerate(store.get_dates())}
                idx = [date_pos[date] for date in scope.read_dates]
                arr = arr[idx]
        return feature_array(
            spec, store, arr, scope=scope, metadata={"source": "memory"}
        )


SmartQuantDataRouter = DataRouter
