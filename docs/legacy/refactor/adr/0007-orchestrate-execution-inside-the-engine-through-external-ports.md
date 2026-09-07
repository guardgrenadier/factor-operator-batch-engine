# ADR 0007：引擎内部统一执行编排，通过外部端口读写数据

- 状态：已接受
- 日期：2026-07-24

> 修订说明（2026-07-27）：ADR 0020 已用固定的 MemoryResultAssembler 和 DiskResultWriter 取代通用 ResultSink；复杂流式消费者和结果评价不进入首版引擎。DiskResultWriter 使用独立设计的 ArtifactStore 端口。

## 背景

当前 chunk 循环位于 `FeatureManager`，叶子读取位于递归 `Executor`，结果写入又由 `Calculator` 和 Store 共同参与。这让研究定义生命周期、数据路由、分块、执行和物化相互耦合，也使多进程难以成为独立执行后端。

## 决策

计算引擎的公共门面负责一次任务的端到端执行编排：

```text
Engine Facade
  -> Compiler
  -> PhysicalPlanner
  -> Scheduler
       -> DataProvider
       -> RuntimeBackend
       -> ResultSink
```

- PhysicalPlanner 与 Scheduler 属于计算引擎的执行子系统。
- DataProvider、SchemaCatalog 和 ResultSink 是外部端口，其具体实现不属于计算核心。
- Runtime 每次只执行一个输入已经绑定的计划分区。
- 调用方提交任务与资源策略，不自行实现 chunk 循环。

## 职责边界

- Compiler 决定算什么，不决定 chunk 和 worker。
- PhysicalPlanner 决定本次怎样分区执行，不改变公式语义。
- Scheduler 协调输入绑定、RuntimeBackend 和结果提交。
- DataProvider 负责逻辑 key 到叶子 source tensor 的读取与对齐，不执行公式空间转换。
- Runtime 只执行分区 DAG 并管理分区内中间值。
- ResultSink 决定结果是返回内存、流式消费还是事务性物化。

## 影响

- `FeatureManager` 不再拥有 chunk 循环和 worker 管理。
- Runtime 不再通过 Store/Router fallback 读取叶子。
- 多进程是可替换 RuntimeBackend，不影响公式、LogicalPlan 或数据端口协议。
- 初期实现可以只支持 date 轴分区，同时保留扩展其他执行后端的边界。
