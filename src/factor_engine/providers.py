"""提供基于内存数据的 DataProvider 实现。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np

from .domain import (
    get_step_values,
    normalize_date_key,
    parse_feature_key,
    stable_hash,
)
from .formula import SourceRefExpr
from .model import (
    DataProviderError,
    DomainError,
    InputSpec,
    NormalizedSourceBatch,
    ReadDomain,
    SourceBinding,
    SourceSpec,
    SourceTerm,
)
from .data_provider.normalize import normalize_source_arrays


class MemoryDataProvider:
    """为契约测试和小型研究任务提供严格的内存数据源。"""

    def __init__(
        self,
        *,
        dates: Sequence[Any],
        asset_codes: Mapping[str, Sequence[Any]],
        data: Mapping[str, Any],
        input_specs: Mapping[str, InputSpec] | None = None,
        load_groups: Mapping[str, str] | None = None,
    ) -> None:
        """使用日期轴、资产代码与三维数据初始化内存数据提供方。"""
        # 规范化日期键并把所有数据统一为 T×N×S 三维数组。
        self._dates = np.asarray([normalize_date_key(value) for value in dates])
        self._codes = {key: np.asarray(value) for key, value in asset_codes.items()}
        self._data = {key: _as_3d(value) for key, value in data.items()}
        self._specs = dict(input_specs or {})
        self._groups = dict(load_groups or {})
        self.load_calls: list[tuple[str, ...]] = []
        self.bound_domains: list[ReadDomain] = []

    def calendar_dates(self, calendar: str) -> np.ndarray:
        """返回内存日期轴作为指定交易日历的有序日期。"""
        return self._dates

    def asset_codes(
        self,
        asset_type: str,
        dates: Sequence[Any] | None = None,
        selector: str | Sequence[Any] = "all",
    ) -> np.ndarray:
        """返回指定资产类型的有序代码主轴。"""
        try:
            return self._codes[asset_type]
        except KeyError as exc:
            raise DomainError(f"Unknown asset type {asset_type!r}") from exc

    def describe_many(
        self, source_refs: Sequence[SourceRefExpr]
    ) -> Mapping[SourceRefExpr, InputSpec]:
        """按逻辑键解析数据源引用的编译期输入规格。"""
        # 先校验逻辑键存在，再取显式输入规格。
        described: dict[SourceRefExpr, InputSpec] = {}
        for ref in source_refs:
            if ref.logical_key not in self._data:
                raise DataProviderError(f"Unknown source {ref.logical_key!r}")
            spec = self._specs.get(ref.logical_key)
            if spec is None:
                # 无显式规格时从字段键和数据形状推导默认输入规格。
                key = parse_feature_key(ref.logical_key)
                spec = InputSpec(
                    key.asset,
                    key.freq,
                    self._data[ref.logical_key].shape[2],
                )
            described[ref] = spec
        return described

    def bind_many(
        self, source_terms: Sequence[SourceTerm], read_domain: ReadDomain
    ) -> Sequence[SourceBinding]:
        """把数据源 Term 批量绑定为内存物理读取描述。"""
        # 记录本次分区读取域，逐个 Term 构造物理源规格。
        self.bound_domains.append(read_domain)
        bindings: list[SourceBinding] = []
        for term in source_terms:
            key = term.source_ref.logical_key
            feature_key = parse_feature_key(key)
            source_spec = SourceSpec.from_key(
                key, source="memory", field=feature_key.name
            )
            domain = term.source_domain
            assert domain.codes is not None
            term_domain = ReadDomain(
                read_domain.dates,
                read_domain.write_dates,
                domain.codes,
                tuple(get_step_values(domain.frequency, domain.step_count)),
                read_domain.output_slice,
            )
            # 依据加载组配置与读取坐标生成稳定的加载组键。
            group = stable_hash(
                self._groups.get(key, key),
                term_domain.dates,
                term_domain.codes,
                term_domain.steps,
            )
            bindings.append(
                SourceBinding(
                    term.term_id,
                    source_spec,
                    term_domain,
                    group,
                    term.value_kind,
                )
            )
        return bindings

    def load_many(self, bindings: Sequence[SourceBinding]) -> NormalizedSourceBatch:
        """按物理绑定从内存数据批量切片加载数组。"""
        # 记录本次加载调用并建立日期与代码的位置索引。
        self.load_calls.append(tuple(binding.term_id for binding in bindings))
        date_positions = {date: i for i, date in enumerate(self._dates.tolist())}
        loaded: dict[str, np.ndarray] = {}
        for binding in bindings:
            source_codes = self._codes[binding.source_spec.asset]
            code_positions = {code: i for i, code in enumerate(source_codes.tolist())}
            try:
                date_index = [
                    date_positions[date] for date in binding.read_domain.dates
                ]
                code_index = [
                    code_positions[code] for code in binding.read_domain.codes
                ]
            except KeyError as exc:
                raise DataProviderError(
                    f"Source {binding.source_spec.key!r} lacks coordinate {exc.args[0]!r}"
                ) from exc
            # 按读取域切片；统一值协议在 Provider 的 Source Load 边界完成。
            data = self._data[binding.source_spec.key]
            loaded[binding.term_id] = data[
                np.ix_(date_index, code_index, range(data.shape[2]))
            ]
        # 内存数据已经按最终坐标排列，只复用稠密数组的统一 Source 值协议。
        return normalize_source_arrays(bindings, loaded)


def _as_3d(value: Any) -> np.ndarray:
    """把输入数组规范化为 T×N×S 三维数组。"""
    array = np.asarray(value)
    if array.ndim == 2:
        array = array[:, :, None]
    if array.ndim != 3:
        raise ValueError(f"Expected T x N x S data, got {array.shape}")
    return array


__all__ = ["MemoryDataProvider"]
