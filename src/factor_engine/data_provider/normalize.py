"""LoadNormalizer：Source 数组进入 Runtime 前的唯一权威规范化边界。

Reader 只返回坐标列或位置提示以及原始值列；这里独占最终职责：
按 ReadDomain 解析并校验坐标、在规范化后的 canonical position 上
跨批次拒绝重复/越界坐标、分配并散布到 T × N × S、转换 float64、
把 NULL/缺失/Infinity 统一为 NaN、校验 MASK 的 0/1/NaN 与 CODE 的
整数/NaN、应用显式默认值与静态日期广播，并返回只读且 term_id 集合
完整的 NormalizedSourceBatch。坐标已对齐的 dense 批次不做散布，
只验证精确形状并授权同一套值协议。
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
    # labels/flat 共享 T*N*S 占用表，static 使用独立的 N 维占用表，
    # 均在规范化后的 canonical position 上跨批次拒绝重复坐标。
    occupied: np.ndarray | None = None
    static_occupied: np.ndarray | None = None
    dense_claimed: set[str] = set()
    for batch in batches:
        if batch.mode == "labels":
            if occupied is None:
                occupied = np.zeros(shape[0] * shape[1] * shape[2], dtype=np.bool_)
            _scatter_labels(bindings, batch.frame, result, domain, shape, occupied)
        elif batch.mode == "static":
            if static_occupied is None:
                static_occupied = np.zeros(shape[1], dtype=np.bool_)
            _scatter_static(bindings, batch.frame, result, domain, static_occupied)
        elif batch.mode == "flat":
            if occupied is None:
                occupied = np.zeros(shape[0] * shape[1] * shape[2], dtype=np.bool_)
            _scatter_flat(bindings, batch.frame, result, shape, occupied)
        elif batch.mode == "dense":
            _scatter_dense(bindings, batch.frame, result, shape, dense_claimed)
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
    shape: tuple[int, int, int],
    occupied: np.ndarray,
) -> None:
    """把 date + asset + 可选 step 标签坐标的批次散布到共同坐标。"""
    if rows.empty:
        return
    date_name, code_name = column(rows, "DataDate"), column(rows, "InnerCode")
    coordinate_names = [date_name, code_name]
    dates = [normalize_date_key(value) for value in rows[date_name]]
    codes = _integer_positions(rows[code_name], "asset")
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
        step_index = _integer_positions(rows[step_name], "step")
        if np.any((step_index < 0) | (step_index >= len(domain.steps))):
            raise DataProviderError("Backend returned step outside ReadDomain")
    # 重复判断基于规范化后的 canonical flat position，并在整个批次流上生效。
    flat_idx = (date_index * shape[1] + asset_index) * shape[2] + step_index
    _claim_positions(flat_idx, occupied)
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
    _claim_positions(flat_idx, occupied)
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
    static_occupied: np.ndarray,
) -> None:
    """把无日期关系批次沿整个任务日期轴广播散布。"""
    if rows.empty:
        return
    code_name = column(rows, "InnerCode")
    codes = _integer_positions(rows[code_name], "asset")
    positions = {int(value): pos for pos, value in enumerate(domain.codes)}
    # 不在任务资产轴上的关系行直接跳过；重复判断只对实际写入的
    # canonical asset position 生效，并在整个批次流上跟踪。
    asset_index = np.asarray(
        [positions.get(int(code), -1) for code in codes], dtype=np.intp
    )
    in_domain = asset_index >= 0
    claimed = asset_index[in_domain]
    ordered = np.sort(claimed)
    if np.any(ordered[1:] == ordered[:-1]) or static_occupied[claimed].any():
        raise DataProviderError("Backend returned duplicate static asset coordinates")
    static_occupied[claimed] = True
    rows.drop(columns=[code_name], inplace=True)
    aliases = _aliases(bindings)
    for binding in bindings:
        converted = values(
            rows[column(rows, aliases[binding.term_id])],
            binding.value_kind,
            binding.source_spec.key,
        )
        result[binding.term_id][:, claimed, 0] = converted[in_domain]


def _scatter_dense(
    bindings: tuple[SourceBinding, ...],
    frame: Any,
    result: dict[str, np.ndarray],
    shape: tuple[int, int, int],
    dense_claimed: set[str],
) -> None:
    """接收坐标已与 ReadDomain 对齐的完整数组，只授权值协议与形状。

    dense 批次不做散布：提供方声明数组坐标已对齐，这里验证精确形状、
    完成 float64/Infinity/ValueKind 值协议后才允许进入 Runtime。
    """
    by_term = {binding.term_id: binding for binding in bindings}
    for term_id, array in frame.items():
        try:
            binding = by_term[term_id]
        except KeyError as exc:
            raise DataProviderError(
                f"Dense batch contains unknown term {term_id!r}"
            ) from exc
        if term_id in dense_claimed:
            raise DataProviderError(
                f"Dense batch provided term {term_id!r} more than once"
            )
        dense_claimed.add(term_id)
        array = np.asarray(array)
        if array.shape != shape:
            raise DataProviderError(
                f"Dense batch for {binding.source_spec.key!r} has shape "
                f"{array.shape}, expected {shape}"
            )
        converted = values(
            array.ravel(), binding.value_kind, binding.source_spec.key
        ).reshape(shape)
        result[term_id][...] = converted


def _claim_positions(flat_idx: np.ndarray, occupied: np.ndarray) -> None:
    """在 canonical flat position 上拒绝同批与跨批重复坐标并登记占用。"""
    ordered = np.sort(flat_idx)
    if np.any(ordered[1:] == ordered[:-1]) or occupied[flat_idx].any():
        raise DataProviderError(
            "Backend returned duplicate date/asset/step coordinates"
        )
    occupied[flat_idx] = True


def _integer_positions(series: pd.Series, coordinate: str) -> np.ndarray:
    """把坐标列解析为严格整数位置，拒绝非数值、非整数与无穷坐标。"""
    numeric = pd.to_numeric(series, errors="raise")
    array = np.asarray(numeric, dtype=np.float64)
    if np.any(~np.isfinite(array)) or np.any(array != np.floor(array)):
        raise DataProviderError(
            f"Backend returned non-integer {coordinate} coordinates"
        )
    return array.astype(np.intp)


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
    """读取绑定声明的显式默认值，未声明时为 NaN。

    显式默认值与物理数据共用同一套 Infinity 与 ValueKind 校验：
    Source 数组里的值不因来自缺行填充而适用不同契约。
    """
    default = binding.source_spec.params.get("default")
    if default is None:
        return np.nan
    return float(values([default], binding.value_kind, binding.source_spec.key)[0])


def _aliases(bindings: Sequence[SourceBinding]) -> dict[str, str]:
    """为每个绑定按顺序生成稳定的 value_序号 结果列别名。"""
    return {
        binding.term_id: f"value_{position}"
        for position, binding in enumerate(bindings)
    }
