"""定义快照存储与旧版研究接口共用的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

import numpy as np

from ...domain import (
    DomainRef,
    ExecutionScope,
    FeatureKey,
    ValueKind,
    ValueSpec,
    _json_default,
    get_ffill_step_index,
    get_freq_step_count,
    get_freq_step_values,
    get_resample_group_index,
    is_intraday_freq,
    normalize_scope_dates,
    parse_intraday_minutes,
    parse_feature_key,
    scope_signature,
    stable_hash,
)
from ...model import SourceSpec

__all__ = [
    "DomainRef",
    "ExecutionScope",
    "FeatureKey",
    "SourceSpec",
    "ValueKind",
    "ValueSpec",
    "_json_default",
    "get_ffill_step_index",
    "get_freq_step_count",
    "get_freq_step_values",
    "get_resample_group_index",
    "is_intraday_freq",
    "normalize_scope_dates",
    "parse_intraday_minutes",
    "parse_feature_key",
    "scope_signature",
    "stable_hash",
]


@dataclass(frozen=True)
class ExecutionRequest:
    """单目标特征执行请求的调度与落盘参数。"""

    target: str
    materialize: bool = True
    overwrite: bool = False
    chunk_size: int | None = None
    overlap: int | None = None
    return_array: bool = True

    def __post_init__(self) -> None:
        """校验单目标执行请求中的调度参数。"""
        # 参数组合在对象创建时一次性校验，执行期无需重复分支。
        if not str(self.target).strip():
            raise ValueError("ExecutionRequest.target must not be empty")
        if self.chunk_size is not None and int(self.chunk_size) <= 0:
            raise ValueError("ExecutionRequest.chunk_size must be positive")
        if self.overlap is not None and int(self.overlap) < 0:
            raise ValueError("ExecutionRequest.overlap must be non-negative")
        if not self.materialize and self.overwrite:
            raise ValueError("ExecutionRequest.overwrite requires materialize=True")
        if not self.materialize and not self.return_array:
            raise ValueError(
                "ExecutionRequest must materialize or return the result array"
            )
        if self.chunk_size is not None and not self.materialize:
            raise ValueError("Chunked execution currently requires materialize=True")
        if self.overlap is not None and self.chunk_size is None:
            raise ValueError("ExecutionRequest.overlap requires chunk_size")


@dataclass(frozen=True)
class FeatureSpace:
    """由日期、资产和 step 三个固定轴构成的特征空间。"""

    asset: str
    freq: str
    dates: np.ndarray
    codes: np.ndarray
    steps: int

    @property
    def key(self) -> str:
        """返回 asset.freq 格式的空间键。"""
        return f"{self.asset}.{self.freq}"

    @property
    def n_dates(self) -> int:
        """返回日期轴长度。"""
        return int(len(self.dates))

    @property
    def n_assets(self) -> int:
        """返回资产轴长度。"""
        return int(len(self.codes))

    @property
    def shape(self) -> tuple[int, int, int]:
        """返回 T x N x S 三维数组形状。"""
        return self.n_dates, self.n_assets, int(self.steps)

    def code_to_pos(self, code: Any) -> int:
        """查找资产代码在固定轴中的列位置。"""
        matches = np.where(self.codes == code)[0]
        if len(matches) == 0:
            raise KeyError(f"Code {code!r} not found in FeatureSpace {self.key}")
        return int(matches[0])


@dataclass
class CalculationResult:
    """Calculator 返回的单次公式计算结果。"""

    key: str
    values: np.ndarray
    space: FeatureSpace
    missing_value: Any = np.nan
    diagnostics: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验 Calculator 返回数组与输出空间一致。"""
        # 统一特征键和第三维后，要求结果精确匹配声明空间。
        self.key = parse_feature_key(self.key).key
        values = np.asarray(self.values)
        if values.ndim == 2:
            values = values[:, :, None]
        if values.shape != self.space.shape:
            raise ValueError(
                f"CalculationResult {self.key} shape {values.shape} does not match space {self.space.shape}"
            )
        self.values = values

    @property
    def asset(self) -> str:
        """从结果键中返回资产类型。"""
        return parse_feature_key(self.key).asset

    @property
    def freq(self) -> str:
        """从结果键中返回频率。"""
        return parse_feature_key(self.key).freq


@dataclass(frozen=True)
class FeatureDef:
    """已注册特征的完整定义与行为配置。"""

    key: str
    asset: str
    freq: str
    name: str
    alias: str | None = None
    formula: Any | None = None
    params: dict[str, Any] = dataclass_field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    steps: int | None = None
    input_mask: Any | None = None
    sample_mask: Any | None = None
    output_mask: Any | None = None
    delay_lf: int = 1
    delay_dict: dict[str, int] = dataclass_field(default_factory=dict)
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)

    @classmethod
    def from_key(
        cls,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        alias: str | None = None,
        formula: Any | None = None,
        steps: int | None = None,
        input_mask: Any | None = None,
        sample_mask: Any | None = None,
        output_mask: Any | None = None,
        delay_lf: int = 1,
        delay_dict: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "FeatureDef":
        """从完整特征键和可选行为参数构造 FeatureDef。"""
        # 键负责提供资产、频率和名称三个规范字段。
        fk = parse_feature_key(key)
        return cls(
            key=fk.key,
            asset=fk.asset,
            freq=fk.freq,
            name=fk.name,
            alias=alias,
            formula=formula,
            params=params or {},
            steps=steps,
            input_mask=input_mask,
            sample_mask=sample_mask,
            output_mask=output_mask,
            delay_lf=int(delay_lf),
            delay_dict=dict(delay_dict or {}),
            metadata=metadata or {},
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FeatureDef":
        """从持久化字典还原 FeatureDef。"""
        # 对缺省集合创建新容器，避免共享可变状态。
        return cls(
            key=payload["key"],
            asset=payload["asset"],
            freq=payload["freq"],
            name=payload["name"],
            alias=payload.get("alias"),
            formula=payload.get("formula"),
            params=payload.get("params", {}),
            dependencies=tuple(payload.get("dependencies", ())),
            steps=payload.get("steps"),
            input_mask=payload.get("input_mask"),
            sample_mask=payload.get("sample_mask"),
            output_mask=payload.get("output_mask"),
            delay_lf=int(payload.get("delay_lf", 1)),
            delay_dict={
                str(key): int(value)
                for key, value in payload.get("delay_dict", {}).items()
            },
            metadata=payload.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """将 FeatureDef 转换为可持久化字典。"""
        # 元组依赖转换为 JSON 友好的列表，其余字段保留原始语义。
        return {
            "key": self.key,
            "asset": self.asset,
            "freq": self.freq,
            "name": self.name,
            "alias": self.alias,
            "formula": self.formula,
            "params": self.params,
            "dependencies": list(self.dependencies),
            "steps": self.steps,
            "input_mask": self.input_mask,
            "sample_mask": self.sample_mask,
            "output_mask": self.output_mask,
            "delay_lf": self.delay_lf,
            "delay_dict": self.delay_dict,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class FeatureMeta:
    """物化特征随数据一起持久化的轴摘要元数据。"""

    key: str
    asset: str
    freq: str
    name: str
    start: str
    end: str
    n_dates: int
    n_assets: int
    steps: int
    dates_hash: str
    codes_hash: str
    feature_def: FeatureDef
    snapshot_id: str | None = None

    @classmethod
    def from_feature(
        cls,
        *,
        key: str,
        space: FeatureSpace,
        start: str,
        end: str,
        dates_hash: str,
        codes_hash: str,
        feature_def: FeatureDef,
        snapshot_id: str | None = None,
    ) -> "FeatureMeta":
        """根据写入空间和 FeatureDef 构造 FeatureMeta。"""
        # 规范键字段，并从实际空间固化两个轴长度和 step 数。
        fk = parse_feature_key(key)
        return cls(
            key=fk.key,
            asset=fk.asset,
            freq=fk.freq,
            name=fk.name,
            start=start,
            end=end,
            n_dates=space.n_dates,
            n_assets=space.n_assets,
            steps=int(space.steps),
            dates_hash=dates_hash,
            codes_hash=codes_hash,
            feature_def=feature_def,
            snapshot_id=snapshot_id,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FeatureMeta":
        """从 metadata 字典还原 FeatureMeta。"""
        # 嵌套特征定义先独立还原，再构造完整元数据对象。
        feature_def_payload = payload.get("feature_def") or {}
        feature_def = FeatureDef.from_dict(feature_def_payload)
        return cls(
            key=payload["key"],
            asset=payload["asset"],
            freq=payload["freq"],
            name=payload["name"],
            start=payload["start"],
            end=payload["end"],
            n_dates=int(payload["n_dates"]),
            n_assets=int(payload["n_assets"]),
            steps=int(payload["steps"]),
            dates_hash=payload["dates_hash"],
            codes_hash=payload["codes_hash"],
            feature_def=feature_def,
            snapshot_id=payload.get("snapshot_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        """将 FeatureMeta 转换为可持久化字典。"""
        # 嵌套定义递归序列化，其余轴摘要直接写出。
        return {
            "key": self.key,
            "asset": self.asset,
            "freq": self.freq,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "n_dates": self.n_dates,
            "n_assets": self.n_assets,
            "steps": self.steps,
            "dates_hash": self.dates_hash,
            "codes_hash": self.codes_hash,
            "feature_def": self.feature_def.to_dict(),
            "snapshot_id": self.snapshot_id,
        }


@dataclass
class FeatureArray:
    """带空间、定义和元数据的三维特征数组。"""

    key: str
    values: np.ndarray
    space: FeatureSpace
    feature_def: FeatureDef | None = None
    feature_meta: FeatureMeta | None = None
    dtype: str | None = None
    missing_value: Any = np.nan
    metadata: dict[str, Any] = dataclass_field(default_factory=dict)
    dims: tuple[str, str, str] = ("date", "asset", "step")

    def __post_init__(self) -> None:
        """在初始化后执行一致性校验。"""
        # 规范键和数组维度，并验证数组与空间完全一致。
        self.key = parse_feature_key(self.key).key
        values = np.asarray(self.values)
        if values.ndim == 2:
            values = values[:, :, None]
        if values.ndim != 3:
            raise ValueError(
                f"FeatureArray values must be 3D T x N x S, got {values.shape}"
            )
        if values.shape != self.space.shape:
            raise ValueError(
                f"FeatureArray {self.key} shape {values.shape} does not match space {self.space.shape}"
            )
        self.values = values
        # 补齐 dtype、布尔缺失值和最小特征定义元数据。
        self.dtype = self.dtype or str(values.dtype)
        if values.dtype.kind == "b" and _is_nan_like(self.missing_value):
            self.missing_value = False
        if self.feature_def is None:
            self.feature_def = FeatureDef.from_key(self.key)

    @property
    def asset(self) -> str:
        """返回特征键中的资产类型。"""
        return parse_feature_key(self.key).asset

    @property
    def freq(self) -> str:
        """返回特征键中的频率。"""
        return parse_feature_key(self.key).freq

    @property
    def name(self) -> str:
        """返回特征键中的名称。"""
        return parse_feature_key(self.key).name

    def with_values(
        self,
        values: np.ndarray,
        *,
        key: str | None = None,
        space: FeatureSpace | None = None,
        feature_def: FeatureDef | None = None,
        feature_meta: FeatureMeta | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "FeatureArray":
        """复用当前空间信息创建替换数值后的 FeatureArray。"""
        # 未覆盖的描述字段沿用当前对象，但新数组仍会经过完整校验。
        return FeatureArray(
            key=key or self.key,
            values=values,
            space=space or self.space,
            feature_def=feature_def or self.feature_def,
            feature_meta=feature_meta or self.feature_meta,
            missing_value=self.missing_value,
            metadata=metadata or dict(self.metadata),
        )


def _is_nan_like(value: Any) -> bool:
    """判断值是否可视为 NaN。"""
    try:
        return bool(np.isnan(value))
    except TypeError:
        return False
