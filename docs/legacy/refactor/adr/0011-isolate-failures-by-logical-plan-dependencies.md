# ADR 0011：按 LogicalPlan 依赖隔离公式失败

- 状态：已接受
- 日期：2026-07-24

> 补充说明（2026-07-27）：ADR 0017 已决定，失败公式此前提交的全部内存和磁盘分区结果必须清理，不作为 partial array 或 artifact 暴露；清理范围仍按 formula ID 隔离。

## 背景

批量计算和自动挖掘需要在一条公式失败时继续执行无关公式。整批公式又会合并为共享 LogicalPlan DAG，因此失败范围不能通过逐公式独立递归求值确定。

## 决策

失败范围按 LogicalPlan 依赖关系决定：

- 一个节点失败时，所有依赖该节点的 formula ID 失败。
- 与失败节点无依赖关系的输出可以继续。
- Compiler、DataProvider 和 Runtime 都产生带 stage 和 formula ID 关联的结构化失败。
- RuntimePartitionResult 可以同时包含成功 OutputChunk 和 FormulaFailure。
- `fail_fast` 与 `continue_independent` 作为 ExecutionOptions 中的 FailurePolicy。

## 失败层级

- Domain 无法解析、执行环境无法建立等属于任务级失败。
- 单条公式语法、算子或空间错误属于公式编译失败。
- 确定的叶子缺失按依赖图影响相关公式；数据服务整体不可用属于可重试的分区或任务基础设施失败。
- Runtime 节点违反执行契约时影响依赖该节点的输出。

约定内的 NaN、missing、非法数学输入转 missing 等属于数值结果，不是执行失败。

## 跨分区语义

一个 formula ID 的任一必要分区失败，则该公式整体未完成。其他公式如果所有必要分区成功，仍可以完成。

已经提交的部分分区全部清理；具体要求见 ADR 0017。

## 影响

- LogicalPlan 需要维护节点到输出 formula ID 的反向依赖。
- Runtime 在 continue_independent 策略下跳过失败节点的后继，并继续健康子图。
- Engine Facade 汇总不同阶段和不同分区的 FormulaFailure。
- worker 崩溃、OOM 等无法安全归因到公式的故障不能伪装成普通公式错误。
