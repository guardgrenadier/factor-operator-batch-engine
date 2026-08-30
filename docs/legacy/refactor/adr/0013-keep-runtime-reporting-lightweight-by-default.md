# ADR 0013：RuntimeReport 默认保持轻量

- 状态：已接受
- 日期：2026-07-24

## 背景

重构需要观察分块执行的耗时和内存，以验证 DAG 生命周期管理和 PhysicalPlan，但自动因子挖掘可能产生大量小节点。默认逐节点 trace、数组质量扫描或进程级内存采样会带来不必要开销。

## 决策

Runtime 每个分区返回非语义的 RuntimeReport。默认 minimal 模式只记录：

- Runtime wall time；
- input tensor bytes；
- output tensor bytes；
- peak tracked tensor bytes；
- executed、skipped、failed node counts。

minimal 模式禁止：

- 为 report 扫描数组值；
- 计算 NaN 比例、min/max/mean/std；
- 轮询 RSS 或启用 tracemalloc；
- 逐节点计时；
- 保存完整执行 trace。

## 开销约束

- wall time 每个分区只读取开始和结束时钟。
- `ndarray.nbytes` 等大小信息通过 O(1) 元数据获取。
- 节点计数和存活字节在 DAG 引用计数更新时同步维护，额外成本为每个节点常数级 bookkeeping。
- 零拷贝 view 的内存统计可以使用底层 storage identity，或明确标记为保守估算；不得通过扫描数组判断共享关系。
- RuntimeReport 保持小型结构，适合 worker 返回给 Scheduler。

## 可选 profiling

ExecutionOptions 可以显式开启 operator profiling，按算子类型聚合：

- call count；
- total/max wall time；
- output bytes。

完整逐节点 trace 和外部进程内存 profiling 不进入第一版常规执行路径。

## 边界

- DataProvider 读取耗时属于数据或 Scheduler 报告。
- worker 排队、重试和并发等待属于 SchedulerReport。
- ResultSink 写入耗时属于 sink 或 JobReport。
- 数值质量和 NaN 分布属于可选结果质量检查。
- profiling 配置不参与公式和结果语义签名。

