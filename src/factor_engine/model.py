"""定义编译期领域、Term 计算图与执行期数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping, Protocol, Sequence

import numpy as np

from .domain import ValueKind, parse_feature_key
from .formula import FormulaBatch, SourceRefExpr


class CompileError(ValueError):
    """编译阶段错误的基类，携带出错阶段标识。"""

    stage = "compile"


class DomainError(CompileError):
    """领域描述解析或校验失败时抛出的错误。"""

    stage = "domain"


class DataProviderError(RuntimeError):
    """数据提供方描述、绑定或加载失败时抛出的错误。"""

    stage = "data_provider"


class RuntimeExecutionError(RuntimeError):
    """运行时执行失败时抛出的错误。"""

    stage = "runtime"


class ResultAssemblyError(RuntimeError):
    """结果汇总阶段失败时抛出的错误。"""

    stage = "result_assembly"


@dataclass(frozen=True)
class DomainSpec:
    """编译期任务范围声明：日期区间、任务资产范围与目标坐标。"""

    start: str
    end: str
    asset_scope: Mapping[str, str | Sequence[Any]]
    target_asset: str
    target_freq: str
    target_step_count: int


@dataclass(frozen=True, eq=False)
class ResolvedOutputDomain:
    """编译后解析出的精确输出域坐标。"""

    dates: np.ndarray
    asset_type: str
    codes: np.ndarray
    frequency: str
    steps: np.ndarray
    calendar: str
    axis_fingerprint: str

    @property
    def shape(self) -> tuple[int, int, int]:
        """返回输出域对应的日期、资产和步长三维形状。"""
        return len(self.dates), len(self.codes), len(self.steps)


@dataclass(frozen=True)
class TermDomain:
    """逻辑计算图中一个值的资产、频率、step 与日历身份。"""

    asset_type: str
    codes: tuple[Any, ...] | None
    frequency: str
    step_count: int
    calendar: str
    axis_fingerprint: str

    @property
    def asset_count(self) -> int:
        """返回物理资产轴长度，其中匿名归约结果固定为 singleton。"""
        return 1 if self.codes is None else len(self.codes)


@dataclass(frozen=True)
class ReadDomain:
    """物理分区实际读取的日期与坐标范围，含历史回看部分。"""

    dates: tuple[str, ...]
    write_dates: tuple[str, ...]
    codes: tuple[Any, ...]
    steps: tuple[Any, ...]
    output_slice: slice = dataclass_field(compare=False, hash=False)


@dataclass(frozen=True)
class InputSpec:
    """供编译使用的数据源输入规格。"""

    asset_type: str
    frequency: str
    step_count: int
    value_kind: ValueKind = ValueKind.NUMERIC
    calendar: str = "default"


@dataclass(frozen=True)
class SourceSpec:
    """分区绑定后产生的中性物理数据源描述。"""

    asset: str
    freq: str
    name: str
    source: str | None = None
    table: str | None = None
    field: str | None = None
    params: Mapping[str, Any] = dataclass_field(default_factory=dict)

    @property
    def key(self) -> str:
        """返回 asset.freq.name 格式的物理源键。"""
        return f"{self.asset}.{self.freq}.{self.name}"

    @classmethod
    def from_key(
        cls,
        key: str,
        *,
        source: str | None = None,
        table: str | None = None,
        field: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> SourceSpec:
        """从 asset.freq.name 键构造物理源规格。"""
        # 先解析键得到字段坐标，再补充可选的物理定位信息。
        parsed = parse_feature_key(key)
        return cls(
            parsed.asset,
            parsed.freq,
            parsed.name,
            source,
            table,
            field,
            dict(params or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回物理源规格的字典表示。"""
        return {
            "key": self.key,
            "asset": self.asset,
            "freq": self.freq,
            "name": self.name,
            "source": self.source,
            "table": self.table,
            "field": self.field,
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class SourceBinding:
    """特定任务中数据源 Term、物理源规格与读取域的绑定。"""

    term_id: str
    source_spec: SourceSpec
    read_domain: ReadDomain
    load_group_key: str
    value_kind: ValueKind = ValueKind.NUMERIC


class DataProvider(Protocol):
    """数据提供方协议：日历、资产轴、输入规格、绑定与加载。"""

    def calendar_dates(self, calendar: str) -> np.ndarray:
        """返回指定交易日历的完整有序日期轴。"""
        ...

    def asset_codes(
        self,
        asset_type: str,
        dates: Sequence[Any] | None = None,
        selector: str | Sequence[Any] = "all",
    ) -> np.ndarray:
        """返回指定任务日期范围和选择器对应的有序代码主轴。"""
        ...

    def describe_many(
        self, source_refs: Sequence[SourceRefExpr]
    ) -> Mapping[SourceRefExpr, InputSpec]:
        """批量描述逻辑数据源的编译期输入契约。"""
        ...

    def bind_many(
        self, source_terms: Sequence[SourceTerm], read_domain: ReadDomain
    ) -> Sequence[SourceBinding]:
        """把数据源 Term 批量绑定为当前分区的物理读取描述。"""
        ...

    def load_many(self, bindings: Sequence[SourceBinding]) -> Mapping[str, np.ndarray]:
        """按物理绑定批量加载以 Term 标识索引的数组。"""
        ...


@dataclass(frozen=True)
class Term:
    """计算图中的规范化可执行节点基类。"""

    term_id: str
    value_kind: ValueKind
    domain: TermDomain | None
    lookback: int
    semantic_key: str


@dataclass(frozen=True)
class LiteralTerm(Term):
    """常量字面量 Term。"""

    value: Any


@dataclass(frozen=True)
class SourceTerm(Term):
    """数据源引用 Term，携带数据源引用与输入规格。"""

    source_ref: SourceRefExpr
    input_spec: InputSpec


@dataclass(frozen=True)
class OperatorTerm(Term):
    """算子 Term，携带输入 Term、具名输入与参数。"""

    operator_name: str
    input_term_ids: tuple[str, ...]
    input_names: tuple[str | None, ...]
    params: Mapping[str, Any]


@dataclass(frozen=True)
class LogicalPlan:
    """包含 Term 依赖、拓扑顺序与输出映射的逻辑计划。"""

    terms: Mapping[str, Term]
    topological_order: tuple[str, ...]
    outputs: Mapping[str, str]
    reference_counts: Mapping[str, int]
    job_lookback: int
    semantic_id: str

    @property
    def source_terms(self) -> tuple[SourceTerm, ...]:
        """按拓扑顺序返回计划中的全部数据源 Term。"""
        return tuple(
            term
            for term_id in self.topological_order
            if isinstance((term := self.terms[term_id]), SourceTerm)
        )


ExecutionPlan = LogicalPlan


@dataclass(frozen=True)
class ComputeRequest:
    """一次批计算任务的输入：领域声明与公式批次。"""

    domain: DomainSpec
    batch: FormulaBatch


@dataclass(frozen=True)
class ExecutionOptions:
    """执行选项，目前支持显式日期分块大小。"""

    chunk_size: int | None = None

    def __post_init__(self) -> None:
        """校验显式日期分块大小必须为正数。"""
        if self.chunk_size is not None and int(self.chunk_size) <= 0:
            raise ValueError("chunk_size must be positive")


@dataclass(frozen=True)
class PhysicalPartition:
    """一次执行分区的编号、输出切片与读取域。"""

    partition_id: int
    output_slice: slice
    read_domain: ReadDomain


@dataclass(frozen=True)
class CompiledJob:
    """编译完成的任务：逻辑计划与解析后的输出域。"""

    plan: LogicalPlan
    domain: ResolvedOutputDomain


@dataclass(frozen=True)
class ResultChunk:
    """单个公式在输出域某个切片上的结果分块。"""

    formula_id: str
    output_slice: slice
    values: np.ndarray


@dataclass
class ExecutionStats:
    """执行统计：加载调用、Workspace 峰值与释放记录。"""

    load_calls: int = 0
    peak_workspace_values: int = 0
    released_terms: list[str] = dataclass_field(default_factory=list)
    provider_events: list[dict[str, Any]] = dataclass_field(default_factory=list)
