"""执行已编译逻辑计划，按物理分区分块产出结果分块并装配完整计算结果。"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterator, Mapping

import numpy as np
import pandas as pd

from .compiler import Compiler
from .domain import ValueKind, as_tristate_mask
from .model import (
    CompiledJob,
    ComputeRequest,
    DataProvider,
    DataProviderError,
    ExecutionOptions,
    ExecutionStats,
    LiteralTerm,
    LogicalPlan,
    NormalizedSourceBatch,
    OperatorTerm,
    PhysicalPartition,
    ReadDomain,
    ResolvedOutputDomain,
    ResultAssemblyError,
    ResultChunk,
    RuntimeExecutionError,
    SourceBinding,
    SourceTerm,
)
from .operators import OperatorSpec, default_operator_registry


class ResultStream(Iterator[ResultChunk]):
    """单次消费的有序结果分块流，仅完整无错消费后才表示成功。"""

    def __init__(
        self,
        domain: ResolvedOutputDomain,
        iterator: Iterator[ResultChunk],
        stats: ExecutionStats,
    ) -> None:
        """用输出域、chunk 迭代器和统计对象创建单次结果流。"""
        # 流状态与底层迭代器一同保存，用于判断是否自然结束。
        self.domain = domain
        self.stats = stats
        self._iterator = iterator
        self._started = False
        self.succeeded = False

    def __iter__(self) -> ResultStream:
        """开始并返回只能消费一次的结果流迭代器。"""
        if self._started:
            raise RuntimeError("ResultStream can only be consumed once")
        self._started = True
        return self

    def __next__(self) -> ResultChunk:
        """返回下一个结果块并在自然结束时标记流成功。"""
        if not self._started:
            self._started = True
        try:
            return next(self._iterator)
        except StopIteration:
            self.succeeded = True
            raise

    def close(self) -> None:
        """在底层迭代器支持时提前关闭结果流。"""
        close = getattr(self._iterator, "close", None)
        if close is not None:
            close()


@dataclass(frozen=True)
class ComputeResult:
    """包含全部公式数组及其共同输出域的完整计算结果。"""

    domain: ResolvedOutputDomain
    arrays: Mapping[str, np.ndarray]
    plan: LogicalPlan
    stats: ExecutionStats

    @property
    def values(self) -> Mapping[str, np.ndarray]:
        """返回按公式标识索引的完整结果数组映射。"""
        return self.arrays

    def to_dataframe(self) -> pd.DataFrame:
        """把结果转换为日期、资产和步长三级索引的 DataFrame。"""
        # 坐标笛卡尔积顺序与三维数组按行展开顺序保持一致。
        index = pd.MultiIndex.from_product(
            [self.domain.dates, self.domain.codes, self.domain.steps],
            names=["date", "asset", "step"],
        )
        return pd.DataFrame(
            {
                formula_id: values.reshape(-1)
                for formula_id, values in self.arrays.items()
            },
            index=index,
        )


BatchResult = ComputeResult


class PhysicalPlanner:
    """把输出日期连同历史回看切分为有序物理分区的规划器。"""

    def __init__(self, provider: DataProvider) -> None:
        """使用数据提供者的交易日历初始化物理规划器。"""
        self.provider = provider

    def partitions(
        self, job: CompiledJob, options: ExecutionOptions
    ) -> tuple[PhysicalPartition, ...]:
        """按日期分块和任务回看长度生成有序物理分区。"""
        # 在完整交易日历上定位输出日期，以便向前扩展回看窗口。
        domain, lookback = job.domain, job.plan.job_lookback
        chunk_size = options.chunk_size or len(domain.dates)
        calendar = np.asarray(self.provider.calendar_dates(domain.calendar))
        positions = {date: i for i, date in enumerate(calendar.tolist())}
        # 每个分区分别记录读取区间、有效写出区间和全局结果切片。
        partitions: list[PhysicalPartition] = []
        for partition_id, start in enumerate(range(0, len(domain.dates), chunk_size)):
            stop = min(start + chunk_size, len(domain.dates))
            write_dates = tuple(domain.dates[start:stop].tolist())
            first = positions[write_dates[0]]
            last = positions[write_dates[-1]]
            read_dates = tuple(calendar[max(0, first - lookback) : last + 1].tolist())
            read_domain = ReadDomain(
                read_dates,
                write_dates,
                tuple(domain.codes.tolist()),
                tuple(domain.steps.tolist()),
                slice(start, stop),
            )
            partitions.append(
                PhysicalPartition(partition_id, slice(start, stop), read_domain)
            )
        return tuple(partitions)


class Runtime:
    """加载数据源绑定并按拓扑顺序执行单个物理分区的运行时。"""

    def __init__(
        self,
        provider: DataProvider,
        operators: Mapping[str, OperatorSpec],
    ) -> None:
        """使用数据提供者和运行时运算符表初始化执行器。"""
        self.provider = provider
        self.operators = operators

    def execute_partition(
        self,
        job: CompiledJob,
        partition: PhysicalPartition,
        stats: ExecutionStats,
    ) -> Iterator[ResultChunk]:
        """绑定并加载数据后按拓扑顺序执行一个物理分区。"""
        # 批量绑定全部数据源，并验证提供者没有遗漏或额外返回。
        plan = job.plan
        bindings = tuple(
            self.provider.bind_many(plan.source_terms, partition.read_domain)
        )
        if {binding.term_id for binding in bindings} != {
            term.term_id for term in plan.source_terms
        }:
            raise DataProviderError(
                "bind_many must return exactly one binding per source term"
            )
        # 相同加载组的数据源合并读取，以降低外部 I/O 次数。
        groups: dict[str, list[SourceBinding]] = {}
        for binding in bindings:
            groups.setdefault(binding.load_group_key, []).append(binding)
        binding_by_term = {binding.term_id: binding for binding in bindings}

        # 按拓扑顺序求值，并依据引用计数及时释放中间数组。
        workspace: dict[str, Any] = {}
        remaining = dict(plan.reference_counts)
        output_ids = set(plan.outputs.values())
        for term_id in plan.topological_order:
            term = plan.terms[term_id]
            if isinstance(term, LiteralTerm):
                workspace[term_id] = np.float64(term.value)
            elif isinstance(term, SourceTerm):
                if term_id not in workspace:
                    # 首次遇到组内任一数据源时一次性加载整个组。
                    group = groups[binding_by_term[term_id].load_group_key]
                    loaded = self.provider.load_many(group)
                    stats.load_calls += 1
                    try:
                        # NormalizedSourceBatch 是 Source Load 边界的可信标记；Runtime
                        # 只核对批次类型和 term_id，不再重复扫描 dtype、shape 或 ValueKind。
                        if not isinstance(loaded, NormalizedSourceBatch):
                            raise DataProviderError(
                                "load_many must return NormalizedSourceBatch"
                            )
                        if set(loaded) != {binding.term_id for binding in group}:
                            raise DataProviderError(
                                "load_many returned incomplete or extra terms"
                            )
                        for binding in group:
                            workspace[binding.term_id] = loaded[binding.term_id]
                    finally:
                        del loaded
            elif isinstance(term, OperatorTerm):
                # 算子只接收已计算输入与编译期固定的配置参数。
                values = [workspace[input_id] for input_id in term.input_term_ids]
                args = [
                    value
                    for name, value in zip(term.input_names, values, strict=True)
                    if name is None
                ]
                keyword_inputs = {
                    name: value
                    for name, value in zip(term.input_names, values, strict=True)
                    if name is not None
                }
                try:
                    value = self.operators[term.operator_name].func(
                        *args, **keyword_inputs, **dict(term.params)
                    )
                except Exception as exc:
                    del values
                    del args
                    del keyword_inputs
                    raise RuntimeExecutionError(
                        f"Operator {term.operator_name!r} failed"
                    ) from exc
                try:
                    workspace[term_id] = _validate_operator_result(
                        value, term, partition
                    )
                finally:
                    del value
                    del values
                    del args
                    del keyword_inputs
                for input_id in term.input_term_ids:
                    remaining[input_id] -= 1
                    if remaining[input_id] == 0 and input_id not in output_ids:
                        workspace.pop(input_id, None)
                        stats.released_terms.append(input_id)
            stats.peak_workspace_values = max(
                stats.peak_workspace_values, len(workspace)
            )

        # 去掉回看前缀，只保留本分区负责写出的日期。
        write_count = len(partition.read_domain.write_dates)
        write_start = len(partition.read_domain.dates) - write_count
        if (
            write_start < 0
            or partition.read_domain.dates[write_start:]
            != partition.read_domain.write_dates
        ):
            raise RuntimeExecutionError(
                "PhysicalPlanner must provide write_dates as a contiguous read_dates suffix"
            )
        write_slice = slice(write_start, len(partition.read_domain.dates))
        expected_shape = (
            write_count,
            len(job.domain.codes),
            len(job.domain.steps),
        )
        for formula_id, term_id in plan.outputs.items():
            values = np.asarray(workspace[term_id])[write_slice]
            try:
                # 最终 singleton 资产轴或 step 轴由 NumPy 零复制广播。
                values = np.broadcast_to(values, expected_shape)
            except ValueError as exc:
                raise RuntimeExecutionError(
                    f"Formula {formula_id!r} produced {values.shape}, expected {expected_shape}"
                ) from exc
            values.setflags(write=False)
            yield ResultChunk(formula_id, partition.output_slice, values)


class BatchFactorEngine:
    """串联编译、物理计划与运行时执行的批量因子计算入口。"""

    def __init__(
        self,
        provider: DataProvider,
        *,
        operators: Mapping[str, OperatorSpec] | None = None,
    ) -> None:
        """使用数据提供者和可选运算符表创建批量因子引擎。"""
        self.provider = provider
        self.operators = dict(operators or default_operator_registry())

    def compile(self, request: ComputeRequest) -> CompiledJob:
        """把计算请求编译为可执行作业而不读取数据。"""
        return Compiler(self.provider, self.operators).compile(request)

    def stream(
        self,
        request: ComputeRequest,
        *,
        options: ExecutionOptions | None = None,
    ) -> ResultStream:
        """编译请求并返回按物理分区惰性执行的单次结果流。"""
        # 编译和物理分区都在首次消费流之前完成。
        job = self.compile(request)
        stats = ExecutionStats()
        events = getattr(self.provider, "diagnostics", None)
        if isinstance(events, list):
            stats.provider_events = events
        partitions = PhysicalPlanner(self.provider).partitions(
            job, options or ExecutionOptions()
        )
        runtime = Runtime(self.provider, self.operators)

        def chunks() -> Iterator[ResultChunk]:
            """按顺序执行所有分区并转发其结果块。"""
            for partition in partitions:
                yield from runtime.execute_partition(job, partition, stats)

        stream = ResultStream(job.domain, chunks(), stats)
        stream.plan = job.plan
        return stream

    def compute(
        self,
        request: ComputeRequest,
        *,
        options: ExecutionOptions | None = None,
    ) -> ComputeResult:
        """完整消费结果流并装配所有公式的最终数组。"""
        # 首次收到某公式的结果块时分配完整输出数组。
        stream = self.stream(request, options=options)
        arrays: dict[str, np.ndarray] = {}
        try:
            for chunk in stream:
                array = arrays.get(chunk.formula_id)
                if array is None:
                    array = np.empty(stream.domain.shape, dtype=np.float64)
                    arrays[chunk.formula_id] = array
                array[chunk.output_slice] = chunk.values
        except Exception:
            arrays.clear()
            raise
        # 只有自然消费到流末尾才允许返回完整结果。
        if not stream.succeeded:
            raise ResultAssemblyError("ResultStream did not complete successfully")
        return ComputeResult(
            stream.domain,
            MappingProxyType(arrays),
            stream.plan,
            stream.stats,
        )


def _validate_operator_result(
    value: Any, term: OperatorTerm, partition: PhysicalPartition
) -> Any:
    """规范并校验运算符结果的形状和非有限值。"""
    # 所有算子结果统一转换为 float64，掩码则校验三态语义。
    array = np.asarray(value, dtype=np.float64)
    if term.value_kind is ValueKind.MASK:
        try:
            as_tristate_mask(array, name=f"Operator {term.operator_name!r} mask")
        except ValueError as exc:
            raise RuntimeExecutionError(str(exc)) from exc
    elif term.value_kind is ValueKind.CODE:
        finite = array[np.isfinite(array)]
        if np.any(finite != np.floor(finite)):
            raise RuntimeExecutionError(
                f"Code operator {term.operator_name!r} contains non-integer values"
            )
    if term.layout.scalar:
        if array.ndim != 0:
            raise RuntimeExecutionError(
                f"Operator {term.operator_name!r} returned shape {array.shape}, expected scalar"
            )
        return array
    # 非标量结果必须精确匹配编译器推导的原生 ArrayLayout。
    expected = (
        len(partition.read_domain.dates),
        term.layout.asset_count,
        term.layout.step_count,
    )
    if array.shape != expected:
        raise RuntimeExecutionError(
            f"Operator {term.operator_name!r} returned shape {array.shape}, expected {expected}"
        )
    # 数值输出只在确有无穷值时复制并改写为缺失值。
    if term.value_kind is not ValueKind.MASK and np.any(np.isinf(array)):
        array = array.copy()
        array[np.isinf(array)] = np.nan
    return array
