"""定义运行时值类型、坐标、频率和稳定身份。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import operator
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np


class ValueKind(str, Enum):
    """表示运行时数组语义类型，物理存储统一使用 float64。"""

    NUMERIC = "numeric"
    MASK = "mask"
    CODE = "code"


def as_tristate_mask(value: Any, *, name: str = "mask") -> np.ndarray:
    """按 0.0/1.0/NaN 协议规范化并校验三值 mask。"""
    array = np.asarray(value, dtype=np.float64)
    invalid = ~np.isnan(array) & (array != 0.0) & (array != 1.0)
    if np.any(invalid):
        examples = np.asarray(array[invalid]).reshape(-1)[:5].tolist()
        raise ValueError(f"{name} must contain only 0.0, 1.0, or NaN; got {examples}")
    return array


def normalize_runtime_axis(value: Any) -> int:
    """校验并返回 T×N×S Runtime 使用的非负轴编号。"""
    # 布尔值虽可转整数但不具备轴语义，因此显式拒绝。
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("axis must be an integer")
    try:
        axis = operator.index(value)
    except TypeError as exc:
        raise ValueError("axis must be an integer") from exc
    if axis not in (0, 1, 2):
        raise ValueError(
            "axis must be one of 0, 1, or 2; negative axes are unsupported"
        )
    return axis


def normalize_periods(value: Any) -> int:
    """校验 delay 家族只使用不依赖未来数据的非负整数周期。"""
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("periods must be an integer")
    try:
        periods = operator.index(value)
    except TypeError as exc:
        raise ValueError("periods must be an integer") from exc
    if periods < 0:
        raise ValueError("periods must be non-negative; future reads are not supported")
    return periods


@dataclass(frozen=True, order=True)
class DomainRef:
    """表示首版批引擎使用的最小静态领域身份。"""

    asset: str
    freq: str

    @property
    def key(self) -> str:
        """返回资产与频率组成的规范领域键。"""
        return f"{self.asset}.{self.freq}"


@dataclass(frozen=True)
class ValueSpec:
    """表示每个批处理 Term 携带的静态值契约。"""

    kind: ValueKind
    domain_ref: DomainRef | None
    physical_dtype: str = "float64"


@dataclass(frozen=True, order=True)
class FeatureKey:
    """以 asset.freq.name 稳定标识数据源字段的键。"""

    asset: str
    freq: str
    name: str

    @property
    def key(self) -> str:
        """返回 asset.freq.name 格式的完整特征键。"""
        return f"{self.asset}.{self.freq}.{self.name}"

    def __str__(self) -> str:
        """返回对象的字符串表示。"""
        return self.key


def parse_feature_key(key: str | FeatureKey) -> FeatureKey:
    """解析并校验 asset.freq.name 格式的完整特征键。"""
    # 名称部分允许包含点号，前两段固定作为资产和频率。
    if isinstance(key, FeatureKey):
        return key
    parts = str(key).split(".")
    if len(parts) < 3:
        raise ValueError(f"Invalid FeatureKey {key!r}; expected asset.freq.name")
    asset, freq = parts[0], parts[1]
    name = ".".join(parts[2:])
    if not asset or not freq or not name:
        raise ValueError(f"Invalid FeatureKey {key!r}; empty asset/freq/name")
    return FeatureKey(asset=asset, freq=freq, name=name)


SUPPORTED_INTRADAY_FREQS = {"1min", "5min", "15min", "30min", "60min"}

SUPPORTED_FREQS = {"1d", *SUPPORTED_INTRADAY_FREQS}

_INTRADAY_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}


def is_intraday_freq(freq: str) -> bool:
    """判断频率是否属于支持的分钟频率。"""
    return freq in SUPPORTED_INTRADAY_FREQS


def parse_intraday_minutes(freq: str) -> int:
    """解析分钟频率中的分钟数。"""
    if freq not in _INTRADAY_MINUTES:
        raise ValueError(f"Unsupported intraday frequency {freq!r}")
    return _INTRADAY_MINUTES[freq]


def get_freq_step_count(freq: str) -> int:
    """返回指定频率每天包含的步长数量。"""
    if freq == "1d":
        return 1
    minutes = parse_intraday_minutes(freq)
    if minutes == 1:
        return 237
    return 240 // minutes


def get_freq_step_values(freq: str) -> np.ndarray:
    """返回指定频率在单个交易日内的步长标签。"""
    if freq == "1d":
        return np.array([0])
    minutes = parse_intraday_minutes(freq)
    if minutes == 1:
        return np.array(_one_minute_start_times(), dtype=int)
    starts_240 = _full_session_start_times()
    return np.array(starts_240[::minutes], dtype=int)


def get_step_values(freq: str, step_count: int) -> np.ndarray:
    """返回标准频率标签，非标准 step 数则返回稳定位置标签。"""
    count = operator.index(step_count)
    if count <= 0:
        raise ValueError("step_count must be positive")
    standard = get_freq_step_values(freq)
    if len(standard) == count:
        return standard
    return np.arange(count, dtype=int)


def get_resample_group_index(
    source_freq: str, target_freq: str
) -> tuple[np.ndarray, int]:
    """生成细频率步长映射到粗频率分组的索引。"""
    # 只允许分钟数整除的细到粗转换，并快速处理同频情况。
    source_minutes = parse_intraday_minutes(source_freq)
    target_minutes = parse_intraday_minutes(target_freq)
    if source_minutes > target_minutes or target_minutes % source_minutes:
        raise ValueError(
            f"Explicit resample requires an integer fine -> coarse conversion, got {source_freq} -> {target_freq}"
        )
    if source_freq == target_freq:
        group_index = np.arange(get_freq_step_count(source_freq))
        return group_index, int(len(group_index))
    # 使用完整交易分钟位置将每个源 step 分配到目标时间桶。
    target_values = get_freq_step_values(target_freq)
    full_values = _full_session_start_times()
    full_pos = {value: i for i, value in enumerate(full_values)}
    group_index = []
    for value in get_freq_step_values(source_freq):
        minute_pos = full_pos[value]
        group_index.append(minute_pos // target_minutes)
    return np.asarray(group_index, dtype=int), int(len(target_values))


def get_ffill_step_index(source_freq: str, target_freq: str) -> np.ndarray:
    """为显式粗到细频率对齐生成前向填充索引。"""
    # 只允许分钟数整除的粗到细转换。
    source_minutes = parse_intraday_minutes(source_freq)
    target_minutes = parse_intraday_minutes(target_freq)
    if source_minutes < target_minutes or source_minutes % target_minutes:
        raise ValueError(
            f"Explicit ffill requires an integer coarse -> fine conversion, got {source_freq} -> {target_freq}"
        )
    # 对每个目标时间点定位不晚于它的最近源 step。
    source_values = get_freq_step_values(source_freq)
    target_values = get_freq_step_values(target_freq)
    source_pos = np.asarray([_time_to_minutes(value) for value in source_values])
    target_pos = np.asarray([_time_to_minutes(value) for value in target_values])
    index = np.searchsorted(source_pos, target_pos, side="right") - 1
    if np.any(index < 0):
        raise ValueError(
            f"Cannot ffill {source_freq} into {target_freq}; target begins before source"
        )
    return index.astype(np.int32)


def _full_session_start_times() -> list[int]:
    """返回完整交易时段的一分钟起始时间。"""
    return _time_range(930, 1129) + _time_range(1300, 1456)


def _one_minute_start_times() -> list[int]:
    """返回上午和下午交易时段的一分钟标签。"""
    return _time_range(930, 1129) + _time_range(1300, 1456)


def _time_range(start: int, end: int) -> list[int]:
    """生成起止时间之间的分钟标签。"""
    # 按小时进位逐分钟生成闭区间的 HHMM 整数标签。
    values: list[int] = []
    hour, minute = divmod(start, 100)
    end_hour, end_minute = divmod(end, 100)
    while (hour, minute) <= (end_hour, end_minute):
        values.append(hour * 100 + minute)
        minute += 1
        if minute == 60:
            hour += 1
            minute = 0
    return values


def _time_to_minutes(value: int) -> int:
    """将 HH:MM 时间转换为当日分钟偏移。"""
    hour, minute = divmod(int(value), 100)
    return hour * 60 + minute


def _json_default(value: Any) -> Any:
    """将 NumPy 和集合类型转换为 JSON 可序列化值。"""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, tuple):
        return list(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def stable_hash(*parts: Any) -> str:
    """对结构化输入生成稳定 SHA-256 摘要。"""
    payload = json.dumps(
        parts, sort_keys=True, default=_json_default, ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_date_key(value: Any) -> str:
    """把日期类值规范化为 Snapshot 使用的存储键格式。"""
    raw = str(value)
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10].replace("-", "")
    return raw.replace("-", "")[:8]


def normalize_scope_dates(dates: Any) -> tuple[str, ...]:
    """把一组日期类值规范化为分区执行使用的日期元组。"""
    if dates is None:
        return ()
    return tuple(normalize_date_key(value) for value in dates)


@dataclass(frozen=True)
class ExecutionScope:
    """描述分区执行的日期读取与写入范围。"""

    read_dates: tuple[str, ...]
    write_dates: tuple[str, ...]
    chunk_id: int | None = None

    def __post_init__(self) -> None:
        """在执行范围创建时规范化读取和写入日期。"""
        object.__setattr__(self, "read_dates", normalize_scope_dates(self.read_dates))
        object.__setattr__(self, "write_dates", normalize_scope_dates(self.write_dates))
        if not self.read_dates:
            raise ValueError("ExecutionScope.read_dates must not be empty")
        if not self.write_dates:
            raise ValueError("ExecutionScope.write_dates must not be empty")


def scope_signature(scope: ExecutionScope | None) -> str | None:
    """返回可选执行范围的稳定缓存签名。"""
    if scope is None:
        return None
    return stable_hash(scope.read_dates, scope.write_dates, scope.chunk_id)
