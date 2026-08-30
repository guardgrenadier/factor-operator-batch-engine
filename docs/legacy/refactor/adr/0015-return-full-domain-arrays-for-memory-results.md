# ADR 0015：内存结果返回完整执行域数组

- 状态：已接受
- 日期：2026-07-27

## 背景

Runtime 可以按日期分区执行，并按 ADR 0014 在各个公式输出节点就绪后独立产生 `OutputChunk`。这些分块适合作为引擎内部的结果交付单位，但如果直接暴露给调用方，调用方就必须理解分区顺序、Domain slice、完整性和拼接规则。

交互式研究和自动因子挖掘需要直接使用 NumPy 数组。它们不应各自重复实现分块装配。

## 决策

启用内存结果时，`ComputeResult.arrays[formula_id]` 是一个与完整 `ResolvedExecutionDomain` 对齐的单个 `ndarray`，不是 `OutputChunk` 列表。

数组 shape 为：

```text
full_date_axis x full_asset_axis x full_step_axis
```

引擎内部使用一个内存结果装配组件接收各个 `OutputChunk`，并把它们放入对应的完整域数组。该组件暂称 `MemoryResultAssembler`。

`OutputChunk` 是内部增量交付协议；调用方不需要根据分区自行拼接内存结果。

## 本 ADR 不决定

- `MemoryResultAssembler` 使用预分配、拼接、memmap 还是其他实现；
- 数组缓冲区的具体分配和释放时机；
- 公式部分分区失败后，未完成内存数组的隐藏或清理方式；
- `ComputeResult` 通过内联坐标、完整 `ResolvedExecutionDomain` 还是稳定引用提供轴信息；
- 仅落盘模式的加载 API 和 ArtifactStore 格式。

## 影响

- 启用内存结果意味着调用方最终可获得完整公式数组，其峰值内存需要通过公式批量大小和执行资源策略控制。
- 仅落盘任务不需要为最终完整结果分配内存数组。
- `MemoryResultAssembler` 不管理 DAG 中间值、不跨任务缓存、不计算 IC，也不承担持久化职责。
- “Writer”不是这个内存组件的核心领域含义；后续协议优先使用能够表达装配职责的命名。
