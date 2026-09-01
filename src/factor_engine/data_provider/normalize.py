"""LoadNormalizer：Source 数组进入 Runtime 前的唯一权威规范化边界。

Reader 只返回坐标列或位置提示以及原始值列；这里独占最终职责：
按 ReadDomain 解析并校验坐标、拒绝重复/越界坐标、分配并散布到
T × N × S、转换 float64、把 NULL/缺失/Infinity 统一为 NaN、校验
MASK 的 0/1/NaN 与 CODE 的整数/NaN、应用显式默认值与静态日期广播，
并返回只读且 term_id 集合完整的 NormalizedSourceBatch。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Sequence

import numpy as np
import pandas as pd

from ..domain import ValueKind, normalize_date_key
from ..model import DataProviderError, SourceBinding
from .backend import column

if TYPE_CHECKING:
    from .readers import RawBatch


def normalize_batches(
    bindings: Sequence[SourceBinding], batches: Iterable[RawBatch]
) -> dict[str, np.ndarray]:
    """消费 Reader 的 RawBatch 序列，完成唯一一次坐标散布与值协议校验。"""
    bindings = tuple(bindings)
    if not bindings:
        return {}
    domain = bindings[0].read_domain
    shape = (len(domain.dates), len(domain.codes), len(domain.steps))
    # 显式默认值来自 source 配置；未声明时缺失位置为 NaN。
    result = {
        binding.term_id: np.full(
            shape, _default_of(binding), dtype=np.float64
        )
        for binding in bindings
    }
    occupied: np.ndarray | None = None
    for batch in batches:
        if batch.mode == "labels":
            _scatter_labels(bindings, batch.frame, result, domain)
        elif batch.mode == "static":
            _scatter_static(bindings, batch.frame, result, domain)
        elif batch.mode == "flat":
            if occupied is None:
                occupied = np.zeros(shape[0] * shape[1] * shape[2], dtype=np.bool_)
            _scatter_flat(bindings, batch.frame, result, shape, occupied)
        else:
            raise DataProviderError(f"Unknown RawBatch coordinate mode {batch.mode!r}")
    for binding in bindings:
        result[binding.term_id].setflags(write=False)
    return result


def _scatter_labels(
    bindings: tuple[SourceBinding, ...],
    rows: pd.DataFrame,
    result: dict[str, np.ndarray],
    domain: Any,
) -> None:
    """把 date + asset + 可选 step 标签坐标的批次散布到共同坐标。"""
    if rows.empty:
        return
    date_name, code_name = column(rows, "DataDate"), column(rows, "InnerCode")
    coordinate_names = [date_name, code_name]
    dates = [normalize_date_key(value) for value in rows[date_name]]
    codes = pd.to_numeric(rows[code_name], errors="raise").astype(int).tolist()
    date_pos = {value: pos for pos, value in enumerate(domain.dates)}
    code_pos = {int(value): pos for pos, value in enumerate(domain.codes)}
    try:
        date_index = np.asarray([date_pos[value] for value in dates], dtype=np.intp)
        asset_index = np.asarray([code_pos[value] for value in codes], dtype=np.intp)
    except KeyError as exc:
        raise DataProviderError(
            f"Backend returned coordinate outside ReadDomain: {exc.args[0]!r}"
        ) from exc
    try:
        step_name = column(rows, "Step")
    except ValueError:
        step_index = np.zeros(len(rows), dtype=np.intp)
    else:
        coordinate_names.append(step_name)
        step_index = pd.to_numeric(rows[step_name], errors="raise").to_numpy(
            dtype=np.intp
        )
        if np.any((step_index < 0) | (step_index >= len(domain.steps))):
            raise DataProviderError("Backend returned step outside ReadDomain")
    if rows.duplicated(coordinate_names).any():
        raise DataProviderError(
            "Backend returned duplicate date/asset/step coordinates"
        )
    rows.drop(columns=coordinate_names, inplace=True)
    aliases = _aliases(bindings)
    for binding in bindings:
        result[binding.term_id][date_index, asset_index, step_index] = values(
            rows[column(rows, aliases[binding.term_id])],
            binding.value_kind,
            binding.source_spec.key,
        )


def _scatter_flat(
    bindings: tuple[SourceBinding, ...],
    batch: Any,
    result: dict[str, np.ndarray],
    shape: tuple[int, int, int],
    occupied: np.ndarray,
) -> None:
    """散布已映射为三维扁平整数位置的批次，并拒绝越界与重复坐标。"""
    total = shape[0] * shape[1] * shape[2]
    flat_idx = np.asarray(batch.column(0).to_numpy(zero_copy_only=False))
    if np.any((flat_idx < 0) | (flat_idx >= total)):
        raise DataProviderError("Backend returned position outside ReadDomain")
    ordered_idx = np.sort(flat_idx)
    if np.any(ordered_idx[1:] == ordered_idx[:-1]) or occupied[flat_idx].any():
        raise DataProviderError(
            "Backend returned duplicate date/asset/step coordinates"
        )
    occupied[flat_idx] = True
    for position, binding in enumerate(bindings, 1):
        converted = values(
            batch.column(position).to_numpy(zero_copy_only=False),
            binding.value_kind,
            binding.source_spec.key,
        )
        result[binding.term_id].reshape(-1)[flat_idx] = converted


def _scatter_static(
    bindings: tuple[SourceBinding, ...],
    rows: pd.DataFrame,
    result: dict[str, np.ndarray],
    domain: Any,
) -> None:
    """把无日期关系批次沿整个任务日期轴广播散布。"""
    if rows.empty:
        return
    code_name = column(rows, "InnerCode")
    if rows.duplicated(code_name).any():
        raise DataProviderError("Backend returned duplicate static asset coordinates")
    positions = {int(value): pos for pos, value in enumerate(domain.codes)}
    codes = pd.to_numeric(rows[code_name], errors="raise").astype(int).tolist()
    rows.drop(columns=[code_name], inplace=True)
    aliases = _aliases(bindings)
    for binding in bindings:
        converted = values(
            rows[column(rows, aliases[binding.term_id])],
            binding.value_kind,
            binding.source_spec.key,
        )
        # 不在任务资产轴上的关系行直接跳过。
        for code, value in zip(codes, converted, strict=True):
            if code in positions:
                result[binding.term_id][:, positions[code], 0] = value


def values(
    series: pd.Series | np.ndarray, kind: ValueKind, source_key: str
) -> np.ndarray:
    """按 Catalog 声明一次性转换并校验 Runtime 值协议。"""
    converted = pd.to_numeric(series, errors="coerce")
    if np.any(pd.notna(series) & pd.isna(converted)):
        raise DataProviderError(f"Source {source_key!r} contains non-numeric values")
    array = np.asarray(converted, dtype=np.float64)
    finite = array[np.isfinite(array)]
    if kind is ValueKind.MASK and np.any((finite != 0.0) & (finite != 1.0)):
        raise DataProviderError(
            f"Mask source {source_key!r} contains values outside 0/1"
        )
    if kind is ValueKind.CODE and np.any(finite != np.floor(finite)):
        raise DataProviderError(
            f"Code source {source_key!r} contains non-integer values"
        )
    if np.any(np.isinf(array)):
        array = array.copy()
        array[np.isinf(array)] = np.nan
    return array


def _default_of(binding: SourceBinding) -> float:
    """读取绑定声明的显式默认值，未声明时为 NaN。"""
    default = binding.source_spec.params.get("default")
    return np.nan if default is None else float(default)


def _aliases(bindings: Sequence[SourceBinding]) -> dict[str, str]:
    """为每个绑定按顺序生成稳定的 value_序号 结果列别名。"""
    return {
        binding.term_id: f"value_{position}"
        for position, binding in enumerate(bindings)
    }
