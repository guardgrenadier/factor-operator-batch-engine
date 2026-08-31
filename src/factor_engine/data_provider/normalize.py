"""RawBatch 进入 Runtime 前的唯一 Source 数组规范化边界。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..domain import ValueKind, normalize_date_key
from ..model import (
    DataProviderError,
    NormalizedSourceBatch,
    RawBatch,
    ReadDomain,
    SourceBinding,
)


class LoadNormalizer:
    """增量消费一个 LoadGroup 的 RawBatch 并原子地产生规范数组。"""

    def __init__(
        self,
        bindings: Sequence[SourceBinding],
        coordinate_mode: str,
    ) -> None:
        """校验 LoadGroup 契约，并按 Source default 预分配最终数组。"""

        self.bindings = tuple(bindings)
        self.coordinate_mode = coordinate_mode
        self.domain, self.term_ids = _binding_contract(self.bindings)
        if coordinate_mode not in {"labels", "flat", "static"}:
            raise DataProviderError(f"Unknown RawBatch coordinate mode {coordinate_mode!r}")
        if coordinate_mode == "static" and len(self.domain.steps) != 1:
            raise DataProviderError("Static RawBatch requires a singleton step axis")

        self.shape = (
            len(self.domain.dates),
            len(self.domain.codes),
            len(self.domain.steps),
        )
        # 先写入完整默认值，Reader 未返回的坐标自然保留 Source 的缺失语义。
        self.arrays = {
            binding.term_id: np.full(
                self.shape,
                _default_value(binding),
                dtype=np.float64,
            )
            for binding in self.bindings
        }
        occupied_size = len(self.domain.codes) if coordinate_mode == "static" else int(
            np.prod(self.shape)
        )
        self.occupied = np.zeros(occupied_size, dtype=np.bool_)

    def normalize(self, batches: Iterable[RawBatch]) -> NormalizedSourceBatch:
        """消费完整流；任一 batch 失败时不交付任何部分数组。"""

        iterator = iter(batches)
        try:
            for batch in iterator:
                # 每个 batch 依次完成协议、坐标、重复和值校验，再写入私有数组。
                coordinates, values, length = self._validate_batch(batch)
                positions = self._positions(coordinates, length)
                self._reject_duplicates(positions)
                for binding in self.bindings:
                    converted = _convert_column(
                        values[binding.term_id],
                        binding.value_kind,
                        binding.source_spec.key,
                    )
                    if self.coordinate_mode == "static":
                        self.arrays[binding.term_id][:, positions, 0] = converted
                    else:
                        self.arrays[binding.term_id].reshape(-1)[positions] = converted
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return _finalize(self.bindings, self.arrays)

    def _validate_batch(
        self, batch: RawBatch
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], int]:
        """校验 RawBatch 模式、键集合、一维列和等长约束。"""

        if not isinstance(batch, RawBatch):
            raise DataProviderError("Reader must yield RawBatch values")
        if batch.coordinate_mode != self.coordinate_mode:
            raise DataProviderError(
                f"Reader yielded {batch.coordinate_mode!r}, expected {self.coordinate_mode!r}"
            )
        coordinate_keys = set(batch.coordinates)
        if self.coordinate_mode == "labels":
            if coordinate_keys not in ({"date", "asset"}, {"date", "asset", "step"}):
                raise DataProviderError("Labels RawBatch requires date, asset, and optional step")
            if "step" not in coordinate_keys and len(self.domain.steps) != 1:
                raise DataProviderError(
                    "Labels RawBatch without step requires a singleton step axis"
                )
        elif self.coordinate_mode == "flat" and coordinate_keys != {"flat_idx"}:
            raise DataProviderError("Flat RawBatch requires only flat_idx")
        elif self.coordinate_mode == "static" and coordinate_keys != {"asset"}:
            raise DataProviderError("Static RawBatch requires only asset")
        if set(batch.values) != self.term_ids:
            raise DataProviderError("RawBatch values must match all LoadGroup term_ids")

        coordinates = {
            key: _one_dimensional(value, f"coordinate {key!r}")
            for key, value in batch.coordinates.items()
        }
        values = {
            key: _one_dimensional(value, f"value {key!r}")
            for key, value in batch.values.items()
        }
        lengths = {len(value) for value in (*coordinates.values(), *values.values())}
        if len(lengths) != 1:
            raise DataProviderError("RawBatch coordinates and values must have equal lengths")
        return coordinates, values, lengths.pop()

    def _positions(
        self,
        coordinates: Mapping[str, np.ndarray],
        length: int,
    ) -> np.ndarray:
        """把 labels、flat 或 static 坐标解析为最终数组的位置。"""

        if self.coordinate_mode == "flat":
            positions = _integer_coordinates(coordinates["flat_idx"], "flat_idx")
            if np.any((positions < 0) | (positions >= int(np.prod(self.shape)))):
                raise DataProviderError("Backend returned position outside ReadDomain")
            return positions

        asset_positions = _label_positions(
            coordinates["asset"], self.domain.codes, "asset"
        )
        if self.coordinate_mode == "static":
            return asset_positions

        date_labels = [normalize_date_key(value) for value in coordinates["date"]]
        date_positions = _label_positions(date_labels, self.domain.dates, "date")
        if "step" in coordinates:
            step_positions = _label_positions(
                coordinates["step"], self.domain.steps, "step"
            )
        else:
            step_positions = np.zeros(length, dtype=np.intp)
        return (
            date_positions * (len(self.domain.codes) * len(self.domain.steps))
            + asset_positions * len(self.domain.steps)
            + step_positions
        )

    def _reject_duplicates(self, positions: np.ndarray) -> None:
        """拒绝当前 batch 内及此前 batch 已占用的重复位置。"""

        if len(np.unique(positions)) != len(positions) or self.occupied[positions].any():
            raise DataProviderError(
                "Backend returned duplicate date/asset/step coordinates"
            )
        self.occupied[positions] = True


def normalize_source_arrays(
    bindings: Sequence[SourceBinding],
    values: Mapping[str, Any],
) -> NormalizedSourceBatch:
    """规范已经按最终位置装配的 Provider 数组，并标记为可信批次。"""

    domain, term_ids = _binding_contract(bindings)
    if set(values) != term_ids:
        raise DataProviderError("Source arrays must match all LoadGroup term_ids")
    shape = (len(domain.dates), len(domain.codes), len(domain.steps))
    arrays: dict[str, np.ndarray] = {}
    for binding in bindings:
        raw = np.asarray(values[binding.term_id])
        if raw.shape != shape:
            raise DataProviderError(
                f"Source {binding.source_spec.key!r} returned shape {raw.shape}, expected {shape}"
            )
        arrays[binding.term_id] = _convert_column(
            raw.reshape(-1), binding.value_kind, binding.source_spec.key
        ).reshape(shape)
    return _finalize(bindings, arrays)


def _binding_contract(
    bindings: Sequence[SourceBinding],
) -> tuple[ReadDomain, set[str]]:
    """确认 bindings 属于同一 LoadGroup、共享 ReadDomain 且 term_id 唯一。"""

    if not bindings:
        raise DataProviderError("LoadNormalizer requires at least one binding")
    first = bindings[0]
    if any(binding.load_group_key != first.load_group_key for binding in bindings[1:]):
        raise DataProviderError("LoadNormalizer requires one LoadGroup")
    if any(binding.read_domain != first.read_domain for binding in bindings[1:]):
        raise DataProviderError("LoadGroup bindings must share one ReadDomain")
    term_ids = {binding.term_id for binding in bindings}
    if len(term_ids) != len(bindings):
        raise DataProviderError("LoadGroup term_ids must be unique")
    return first.read_domain, term_ids


def _default_value(binding: SourceBinding) -> float:
    """按 Source ValueKind 规范并校验单个默认值。"""

    return float(
        _convert_column(
            np.asarray([binding.source_spec.default], dtype=object),
            binding.value_kind,
            binding.source_spec.key,
        )[0]
    )


def _one_dimensional(value: Any, name: str) -> np.ndarray:
    """把 pandas、Arrow 或 NumPy 列转为一维 NumPy 视图。"""

    if hasattr(value, "to_numpy") and not isinstance(value, np.ndarray):
        try:
            array = value.to_numpy(zero_copy_only=False)
        except TypeError:
            array = value.to_numpy()
    else:
        array = np.asarray(value)
    if array.ndim != 1:
        raise DataProviderError(f"RawBatch {name} must be one-dimensional")
    return array


def _integer_coordinates(values: np.ndarray, name: str) -> np.ndarray:
    """把位置坐标转换为有限整数索引。"""

    converted = pd.to_numeric(values, errors="coerce")
    array = np.asarray(converted, dtype=np.float64)
    if np.any(~np.isfinite(array)) or np.any(array != np.floor(array)):
        raise DataProviderError(f"RawBatch {name} must contain finite integers")
    return array.astype(np.intp)


def _label_positions(values: Iterable[Any], labels: Sequence[Any], name: str) -> np.ndarray:
    """把标签列映射到冻结轴位置，并拒绝 ReadDomain 外的标签。"""

    positions = {value: index for index, value in enumerate(labels)}
    result: list[int] = []
    try:
        for raw in values:
            value = raw.item() if isinstance(raw, np.generic) else raw
            try:
                result.append(positions[value])
            except (KeyError, TypeError):
                result.append(positions[int(value)])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataProviderError(
            f"Backend returned {name} coordinate outside ReadDomain"
        ) from exc
    return np.asarray(result, dtype=np.intp)


def _convert_column(values: Any, kind: ValueKind, source_key: str) -> np.ndarray:
    """统一数值化、Infinity 处理和 MASK/CODE 有限值校验。"""

    raw = np.asarray(values)
    converted = pd.to_numeric(raw, errors="coerce")
    if np.any(pd.notna(raw) & pd.isna(converted)):
        raise DataProviderError(f"Source {source_key!r} contains non-numeric values")
    array = np.asarray(converted, dtype=np.float64)
    if np.any(np.isinf(array)):
        array = array.copy()
        array[np.isinf(array)] = np.nan
    finite = array[np.isfinite(array)]
    if kind is ValueKind.MASK and np.any((finite != 0.0) & (finite != 1.0)):
        raise DataProviderError(
            f"Mask source {source_key!r} contains values outside 0/1"
        )
    if kind is ValueKind.CODE and np.any(finite != np.floor(finite)):
        raise DataProviderError(
            f"Code source {source_key!r} contains non-integer values"
        )
    return array


def _finalize(
    bindings: Sequence[SourceBinding], arrays: Mapping[str, np.ndarray]
) -> NormalizedSourceBatch:
    """复核 term_id、shape、dtype，并交付只读规范批次。"""

    if set(arrays) != {binding.term_id for binding in bindings}:
        raise DataProviderError("Normalized Source arrays have incomplete term_ids")
    domain = bindings[0].read_domain
    shape = (len(domain.dates), len(domain.codes), len(domain.steps))
    finalized: dict[str, np.ndarray] = {}
    for binding in bindings:
        array = arrays[binding.term_id]
        if array.dtype != np.float64 or array.shape != shape:
            raise DataProviderError("Normalized Source array has invalid dtype or shape")
        array.flags.writeable = False
        finalized[binding.term_id] = array
    return NormalizedSourceBatch(finalized)


__all__ = ["LoadNormalizer", "normalize_source_arrays"]
