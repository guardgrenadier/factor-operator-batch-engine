"""把物理查询返回的长表结果严格散布到绑定共同坐标与值协议。"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ..domain import ValueKind, normalize_date_key
from ..model import DataProviderError, SourceBinding
from .backend import column


def scatter_rows(
    bindings: Sequence[SourceBinding],
    rows: pd.DataFrame,
    fields: Mapping[str, str],
    *,
    step_col: str | None = None,
    constants: Mapping[str, float] | None = None,
    defaults: Mapping[str, float] | None = None,
) -> dict[str, np.ndarray]:
    """将规范长表严格散布到一组 binding 的共同坐标。"""

    if rows.empty:
        return _empty(bindings, defaults)
    domain = bindings[0].read_domain
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
    if step_col is None:
        step_index = np.zeros(len(rows), dtype=np.intp)
    else:
        step_name = column(rows, step_col)
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
    result = _empty(bindings, defaults)
    constants = constants or {}
    for binding in bindings:
        if binding.term_id in constants:
            result[binding.term_id][date_index, asset_index, step_index] = constants[
                binding.term_id
            ]
        else:
            result[binding.term_id][date_index, asset_index, step_index] = values(
                rows[column(rows, fields[binding.term_id])],
                binding.value_kind,
                binding.source_spec.key,
            )
    return result


def scatter_positions(
    bindings: Sequence[SourceBinding],
    rows: pd.DataFrame,
    fields: Mapping[str, str],
) -> dict[str, np.ndarray]:
    """散布已由物理查询映射为三维整数位置的结果。"""

    if rows.empty:
        return _empty(bindings)
    names = [column(rows, name) for name in ("date_idx", "asset_idx", "step_idx")]
    if rows.duplicated(names).any():
        raise DataProviderError(
            "Backend returned duplicate date/asset/step coordinates"
        )
    indices = tuple(
        pd.to_numeric(rows[name], errors="raise").to_numpy(dtype=np.intp)
        for name in names
    )
    domain = bindings[0].read_domain
    shape = (len(domain.dates), len(domain.codes), len(domain.steps))
    if any(
        np.any((index < 0) | (index >= shape[axis]))
        for axis, index in enumerate(indices)
    ):
        raise DataProviderError("Backend returned position outside ReadDomain")
    rows.drop(columns=names, inplace=True)
    result = _empty(bindings)
    for binding in bindings:
        result[binding.term_id][indices] = values(
            rows[column(rows, fields[binding.term_id])],
            binding.value_kind,
            binding.source_spec.key,
        )
    return result


def scatter_static(
    bindings: Sequence[SourceBinding],
    rows: pd.DataFrame,
    fields: Mapping[str, str],
    *,
    prepared: Mapping[str, pd.Series | np.ndarray] | None = None,
) -> dict[str, np.ndarray]:
    """将无日期关系沿任务日期轴广播。"""

    if rows.empty:
        return _empty(bindings)
    code_name = column(rows, "InnerCode")
    if rows.duplicated(code_name).any():
        raise DataProviderError("Backend returned duplicate static asset coordinates")
    domain = bindings[0].read_domain
    positions = {int(value): pos for pos, value in enumerate(domain.codes)}
    codes = pd.to_numeric(rows[code_name], errors="raise").astype(int).tolist()
    rows.drop(columns=[code_name], inplace=True)
    result = _empty(bindings)
    prepared = prepared or {}
    for binding in bindings:
        source = (
            prepared[binding.term_id]
            if binding.term_id in prepared
            else rows[column(rows, fields[binding.term_id])]
        )
        converted = values(
            source,
            binding.value_kind,
            binding.source_spec.key,
        )
        for code, value in zip(codes, converted, strict=True):
            if code in positions:
                result[binding.term_id][:, positions[code], 0] = value
    return result


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


def _empty(
    bindings: Sequence[SourceBinding],
    defaults: Mapping[str, float] | None = None,
) -> dict[str, np.ndarray]:
    """为每个绑定创建读取域形状的数组，用默认值或 NaN 填充。"""

    defaults = defaults or {}
    return {
        binding.term_id: np.full(
            (
                len(binding.read_domain.dates),
                len(binding.read_domain.codes),
                len(binding.read_domain.steps),
            ),
            defaults.get(binding.term_id, np.nan),
            dtype=np.float64,
        )
        for binding in bindings
    }
