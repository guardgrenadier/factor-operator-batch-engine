"""旧版实现：把长表数据对齐到快照固定轴的公共工具。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ...domain import ExecutionScope, get_freq_step_count, get_freq_step_values
from .model import (
    FeatureArray,
    FeatureDef,
    SourceSpec,
)
from .store import FeatureStore


def feature_from_frame(
    spec: SourceSpec,
    store: FeatureStore,
    df: pd.DataFrame,
    *,
    value_col: str,
    step_col: str | None = None,
    step_count: int = 1,
    dtype: Any = float,
    missing_value: Any = np.nan,
    metadata: dict[str, Any] | None = None,
    scope: ExecutionScope | None = None,
) -> FeatureArray:
    """将日频长表按 Snapshot 日期和资产轴对齐为三维特征数组。"""
    # 先解析目标空间并把字段统一编码成可写入数组的数值类型。
    space = store.resolve_space(
        FeatureDef.from_key(spec.key, steps=step_count), scope=scope
    )
    df, value_metadata, dtype, missing_value = normalize_value_column(
        df, value_col, dtype=dtype, missing_value=missing_value
    )
    metadata = {**(metadata or {}), **value_metadata}
    # 预填缺失值，再将有效长表记录散布到日期、资产和 step 位置。
    values = np.full(
        (space.n_dates, space.n_assets, step_count), missing_value, dtype=dtype
    )
    if not df.empty:
        dates = _date_indexer(df["DataDate"], space.dates)
        code_axis = pd.Index(space.codes)
        codes = code_axis.get_indexer(
            _normalize_code_series(df["InnerCode"], space.codes)
        )
        if step_col is None:
            valid = (dates >= 0) & (codes >= 0)
            values[dates[valid], codes[valid], 0] = df.loc[valid, value_col].to_numpy()
        else:
            steps_array = pd.to_numeric(df[step_col], errors="coerce").to_numpy()
            valid = (
                (dates >= 0)
                & (codes >= 0)
                & pd.notna(steps_array)
                & (steps_array >= 0)
                & (steps_array < step_count)
            )
            values[
                dates[valid],
                codes[valid],
                steps_array[valid].astype(int),
            ] = df.loc[valid, value_col].to_numpy()
    return feature_array(
        spec, store, values, missing_value=missing_value, metadata=metadata, scope=scope
    )


def feature_from_intraday_frame(
    spec: SourceSpec,
    store: FeatureStore,
    df: pd.DataFrame,
    *,
    value_col: str,
    step_col: str,
    dtype: Any = float,
    missing_value: Any = np.nan,
    metadata: dict[str, Any] | None = None,
    scope: ExecutionScope | None = None,
) -> FeatureArray:
    """将分钟长表按 Snapshot 日期、资产和日内步长对齐为三维数组。"""
    # 为三个坐标轴建立位置索引，并按目标 dtype 预分配数组。
    step_values = get_freq_step_values(spec.freq)
    space = store.resolve_space(spec, scope=scope)
    df, value_metadata, dtype, missing_value = normalize_value_column(
        df, value_col, dtype=dtype, missing_value=missing_value
    )
    metadata = {**(metadata or {}), **value_metadata}
    date_pos = {date_storage_key(date): i for i, date in enumerate(space.dates)}
    code_pos = {code: i for i, code in enumerate(space.codes)}
    step_pos = {int(step): i for i, step in enumerate(step_values)}
    values = np.full(
        (space.n_dates, space.n_assets, get_freq_step_count(spec.freq)),
        missing_value,
        dtype=dtype,
    )
    # 仅将三个坐标都能命中的记录写入标准空间。
    if not df.empty:
        dates = df["DataDate"].map(date_storage_key).map(date_pos)
        codes = df["InnerCode"].map(code_pos)
        steps = pd.to_numeric(df[step_col], errors="coerce").map(step_pos)
        valid = dates.notna() & codes.notna() & steps.notna()
        values[
            dates.loc[valid].to_numpy(dtype=int),
            codes.loc[valid].to_numpy(dtype=int),
            steps.loc[valid].to_numpy(dtype=int),
        ] = df.loc[valid, value_col].to_numpy()
    return feature_array(
        spec, store, values, missing_value=missing_value, metadata=metadata, scope=scope
    )


def feature_array(
    spec: SourceSpec,
    store: FeatureStore,
    values: np.ndarray,
    *,
    missing_value: Any = np.nan,
    metadata: dict[str, Any] | None = None,
    scope: ExecutionScope | None = None,
) -> FeatureArray:
    """把已对齐数组封装为带空间和来源信息的 FeatureArray。"""
    # 日频二维输入补齐 singleton step 轴，其余输入必须严格为三维。
    values = np.asarray(values)
    if values.ndim == 2:
        values = values[:, :, None]
    if values.ndim != 3:
        raise ValueError(
            f"Source values for {spec.key} must be 2D or 3D, got {values.shape}"
        )
    # 基本面 quarters 或非标准第三维会显式写入特征定义。
    default_steps = get_freq_step_count(spec.freq)
    explicit_steps = int(values.shape[2])
    spec_steps = (
        int(spec.params["quarters"])
        if spec.source == "Fundamental" and "quarters" in spec.params
        else None
    )
    feature_steps = (
        spec_steps
        if spec_steps is not None
        else (explicit_steps if explicit_steps != default_steps else None)
    )
    feature_def = FeatureDef.from_key(
        spec.key,
        steps=feature_steps,
    )
    space = store.resolve_space(feature_def, scope=scope)
    return FeatureArray(
        key=spec.key,
        values=values,
        space=space,
        feature_def=feature_def,
        missing_value=missing_value,
        metadata={
            "kind": "source",
            "source": spec.source,
            "table": spec.table,
            "field": spec.field,
            "params": spec.params,
            **(metadata or {}),
        },
    )


def date_bounds(space) -> tuple[str, str]:
    """返回特征空间覆盖的首尾 SQL 日期。"""
    dates = [date_sql_key(date) for date in space.dates]
    return min(dates), max(dates)


def normalize_value_column(
    df: pd.DataFrame,
    value_col: str,
    *,
    dtype: Any,
    missing_value: Any,
) -> tuple[pd.DataFrame, dict[str, Any], Any, Any]:
    """将数值字符串或分类字段转换为可写入数组的数值列。"""
    # 原生数值列与布尔目标不需要额外编码。
    if df.empty or dtype is bool:
        return df, {}, dtype, missing_value
    series = df[value_col]
    if pd.api.types.is_numeric_dtype(series):
        return df, {}, dtype, missing_value
    # 全部非空值可转数字时，保留其数值字符串来源信息。
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == series.notna().sum():
        out = df.copy()
        out[value_col] = numeric
        return (
            out,
            {"original_dtype": str(series.dtype), "encoding": "numeric_string"},
            float,
            np.nan,
        )
    # 其余文本按稳定排序编码为从一开始的分类编号。
    categories = sorted(str(value) for value in pd.unique(series.dropna()))
    mapping = {value: i + 1 for i, value in enumerate(categories)}
    out = df.copy()
    out[value_col] = series.map(
        lambda value: mapping.get(str(value), np.nan) if pd.notna(value) else np.nan
    )
    return (
        out,
        {
            "original_dtype": str(series.dtype),
            "encoding": "category",
            "category_mapping": mapping,
        },
        float,
        np.nan,
    )


def _date_storage_series(series: pd.Series) -> pd.Series:
    """向量化地把日期类列规范化为 YYYYMMDD 存储格式。"""
    # 日期类型与整数型日期使用无损的快速转换路径。
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.strftime("%Y%m%d")
    if pd.api.types.is_numeric_dtype(series):
        numeric = pd.to_numeric(series, errors="coerce")
        finite = numeric.dropna()
        if finite.empty or np.all(np.equal(np.mod(finite.to_numpy(dtype=float), 1), 0)):
            return numeric.astype("Int64").astype("string")
    # 文本先处理常见分隔符，仅对异常值回退到通用日期解析。
    text = series.astype("string").str.strip()
    normalized = (
        text.str.slice(0, 10)
        .str.replace("-", "", regex=False)
        .str.replace("/", "", regex=False)
        .str.slice(0, 8)
    )
    invalid = normalized.notna() & ~normalized.str.match(r"^\d{8}$", na=False)
    if invalid.any():
        parsed = pd.to_datetime(series, errors="coerce")
        normalized.loc[invalid] = parsed.dt.strftime("%Y%m%d").loc[invalid]
    return normalized


def _date_indexer(series: pd.Series, axis_dates: np.ndarray) -> np.ndarray:
    """仅规范化唯一日期并返回各行在日期轴上的位置。"""
    # 先因子化原始列，避免对重复日期反复执行格式转换。
    date_axis = pd.Index(_date_storage_series(pd.Series(axis_dates)))
    codes, uniques = pd.factorize(series, sort=False)
    unique_pos = date_axis.get_indexer(_date_storage_series(pd.Series(uniques)))
    out = np.full(len(series), -1, dtype=np.intp)
    valid = codes >= 0
    out[valid] = unique_pos[codes[valid]]
    return out


def _normalize_code_series(series: pd.Series, axis_codes: np.ndarray) -> pd.Series:
    """把内部代码值规范化为资产轴使用的数据类型族。"""
    codes = np.asarray(axis_codes)
    if np.issubdtype(codes.dtype, np.number):
        return pd.to_numeric(series, errors="coerce")
    return series


def date_lookup_keys(value: Any) -> tuple[str, ...]:
    """生成日期值可用于匹配的多种规范化文本。"""
    # 同时保留原值、日期前缀和两种常见日期格式并稳定去重。
    raw = str(value)
    keys = [raw]
    if len(raw) >= 10:
        keys.append(raw[:10])
    try:
        dt = pd.to_datetime(value)
        keys.append(dt.strftime("%Y-%m-%d"))
        keys.append(dt.strftime("%Y%m%d"))
    except Exception:
        pass
    return tuple(dict.fromkeys(keys))


def date_sql_key(value: Any) -> str:
    """将日期转换为 SQL 查询使用的 YYYY-MM-DD 格式。"""
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        raw = str(value)
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        return raw[:10]


def date_storage_key(value: Any) -> str:
    """将日期转换为文件存储使用的 YYYYMMDD 格式。"""
    try:
        return pd.to_datetime(value).strftime("%Y%m%d")
    except Exception:
        return str(value).replace("-", "")[:8]
