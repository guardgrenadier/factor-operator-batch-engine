# ADR 0010：Runtime 只返回分区计算结果

- 状态：已接受
- 日期：2026-07-24

> 修订说明（2026-07-27）：ADR 0014 已取代“必须等待 Runtime 一次性返回当前分区全部输出”这一交付粒度。分区仍是计算、坐标和报告边界，但挂有 `formula_id` 的输出节点就绪后可以独立进入结果处理。本文其余关于输出空间和 Runtime 边界的决定继续有效。

## 背景

当前 `FeatureArray` 同时携带数组、FeatureSpace、`FeatureDef`、物化 metadata 和其他研究信息。这使 Runtime 输出与研究定义、Store 和最终结果形式耦合。

一个批次具有统一 ExecutionDomain，物理执行则按 output/read domain 分区。Runtime 只需要交出当前分区的多个公式结果。

## 决策

Runtime 每次返回 RuntimePartitionResult：

```text
RuntimePartitionResult
├── partition_id
├── resolved_domain_id
├── output_slice
├── outputs: formula_id -> OutputChunk
└── runtime_report
```

- 所有 OutputChunk 共享当前分区的 output Domain slice。
- 每个成功输出的 shape 为 `write_dates x full_asset_axis x full_step_axis`。
- Runtime 可以使用扩展的 read dates 计算，但只返回 write dates。
- Runtime 返回后不再修改输出数组；数组所有权转交给 Scheduler。
- RuntimeReport 与计算数组分离。

## 不属于 Runtime 输出的内容

- `FeatureDef`、alias 和研究 metadata；
- 物化位置、overwrite 和 Store manifest；
- 完整结果 lineage；
- 面向研究者的最终 FeatureArray；
- 跨分区拼接后的完整结果。

Engine Facade 和 ResultSink 可以根据 ComputeRequest、计划、ResolvedExecutionDomain、输入 lineage 和分区结果构造最终研究对象或物化产物。

## 影响

- `FeatureArray` 可以保留为研究 API 或内存 sink 的最终包装对象，但不作为 Runtime 内部协议。
- ResultSink 可以流式消费 OutputChunk，而无需等待完整区间进入内存。
- Scheduler 可以在 sink 消费完成后释放分区输出。
- 公式级失败在 RuntimePartitionResult 中如何表达另行决策。
