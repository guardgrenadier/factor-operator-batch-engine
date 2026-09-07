# ADR 0020：使用固定结果处理组件并把复杂后处理留在引擎外

- 状态：已接受
- 日期：2026-07-27

## 背景

早期设计用通用 `ResultSink` 同时表示内存返回、流式消费和事务性物化，容易把任意回调、评价指标、外部消费协议和存储事务都引入计算引擎。

首版实际需要的结果目的地只有内存和磁盘。IC、质量评价和其他后处理可以在任务返回后读取内存数组或磁盘 artifact，由外部模块完成。

ADR 0014—0019 已经分别确定输出节点交付、完整内存数组、结果坐标、失败清理、公式级 finalize 和整批 OutputOptions。

## 决策

首版不提供通用 ResultConsumer、任意外部回调或引擎内评价指标。

Scheduler 只协调两个固定、任务级结果组件：

```text
Scheduler
├── MemoryResultAssembler     # OutputOptions.memory=true
└── DiskResultWriter          # OutputOptions.disk=true
       └── ArtifactStore
```

- `MemoryResultAssembler` 把 OutputChunk 装配为完整域 ndarray。
- `DiskResultWriter` 管理 begin、逐块写入、公式级 finalize 和 abort 生命周期。
- `ArtifactStore` 负责具体布局、编码、读取、增量更新和原子发布能力；它与 DiskResultWriter 的接收时机和生命周期协议分开设计。
- 两个结果组件都未启用时是否允许执行，由后续 API 校验决定。

任务结束统一返回：

```text
ComputeResult
├── domain: ResolvedExecutionDomain
├── arrays
├── artifacts
├── failures
└── job_report
```

复杂后处理通过消费 `ComputeResult.arrays`，或通过 ArtifactStore/load API 读取 artifact，在引擎外完成。

## 影响

- ADR 0007 和术语表中“ResultSink 支持任意流式消费”的表述被本 ADR 取代。
- 自动因子挖掘首版通过批量大小控制完整内存结果的峰值，并在引擎外计算 IC。
- 高频研究可以仅写盘，低频研究可以同时使用内存和磁盘。
- ResultWriter 协议不决定 ArtifactStore 使用每日 NPY、稠密分块、Zarr、Parquet 或其他格式。

## 推迟到实现设计

- RuntimeBackend 与 Scheduler 之间使用同步调用、iterator 还是 queue；
- OutputChunk 的借用、复制、共享引用或所有权转移；
- memory 与 disk 同时启用时的局部设施故障处理；
- 写入背压、异步化和进程间传输；
- 自动重试和崩溃恢复。
