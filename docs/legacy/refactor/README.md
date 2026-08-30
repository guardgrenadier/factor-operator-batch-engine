# 因子计算引擎重构讨论

本目录记录因子计算引擎重构过程中已经确认的约束、架构决策和仍待回答的问题。

当前阶段是领域建模与顶层架构讨论。标记为“拟议”的 ADR 不是最终实现承诺；只有在关键协议达成共识后，才会进入代码迁移设计。

## 批量计算落地提案

基于本文远期架构、当前项目实现和 Zipline Pipeline 执行模型形成了一版批量因子计算
落地草案：

- [逐条设计决策](batch-engine-proposal/decisions.md)
- [架构设计](batch-engine-proposal/architecture.md)
- [完整实现演进 Handoff](batch-engine-proposal/implementation-handoff.md)

这些文档仍处于草案状态，用于区分已经明确的需求、建议接受的设计和待决定问题，
不取代本目录中已经接受的 ADR。

## 已确认的使用场景

1. **交互式研究**
   - 研究者先选择研究区间和资产轴。
   - 研究者声明或组合因子，不自行读取底层数据。
   - 需要保留当前面向个人研究的定义、别名、helper 和物化能力。
2. **已知公式批量计算**
   - 一次提交一批字符串公式。
   - 公式可以直接引用逻辑数据 key。
   - 不要求为每条公式预先创建 `FeatureDef`。
3. **自动因子挖掘**
   - 计算形态与字符串公式批量计算相近。
   - 需要低成本、结构化的合法性检查。
   - 性能、内存控制和批量失败隔离是重要约束。

## 当前核心判断

- 三个使用场景应复用同一套公式编译与执行内核。
- 交互式研究与批任务是不同的上层入口，不是不同的表达式语义。
- 自动挖掘是批任务入口的强化形态，不应形成第三套执行路径。
- 数据路由和底层数据源选择应与公式执行分离。
- “执行时允许按需加载”与“执行器递归到叶子时自行路由取数”不是同一件事；前者可以由调度器通过显式输入协议实现，后者应被移除。
- 一个批次共享统一的资产类型、输出频率、日期区间和资产轴；需要其他计算域时拆成另一个批次。
- 引擎负责判断公式是否能被其语言和执行模型正确执行；挖掘算法负责判断候选公式是否符合搜索空间、业务意义和研究约束。
- 有状态研究平台包裹任务级、近似无状态的计算引擎；研究定义和长期基础设施状态不进入引擎对象。
- Runtime 不进行隐式语义转换；唯一、非聚合的跨资产或跨频率投影可以在编译期自动展开，多个源坐标归并到一个目标坐标时必须显式声明聚合或选择。
- 引擎不自动引入 delay 等 PIT 政策；日频到日内可以自动展开结构广播，但引用日期由上游公式显式决定。
- 研究 helper 与字符串 Parser 只是不同输入适配方式；所有入口汇入同一种 Surface AST，并经过同一个多阶段 Compiler 生成唯一的 Canonical IR 和 LogicalPlan。
- PhysicalPlanner 与 Scheduler 属于引擎的执行子系统；DataProvider 与 ArtifactStore 是外部端口。Scheduler 按 OutputOptions 协调固定的 MemoryResultAssembler 和 DiskResultWriter，不提供通用 ResultConsumer。
- 研究、批量和挖掘入口最终构造同一种 ComputeRequest；请求只描述统一计算域和带 ID 的公式集合，不携带研究状态、数据路由、物化意图或资源参数。
- 一个任务使用固定、有序的日期轴、资产轴和 step 轴；动态上市状态、指数成分和可交易性通过特征或 mask 表达。DomainSpec 在编译前解析为不可变的 ResolvedExecutionDomain。
- 结果层只观察挂有 formula_id 的输出节点；某个输出节点的分区数组就绪后可以独立进入结果处理，不必等待同一分区的其他公式。普通 DAG 中间节点不对结果层暴露。
- 启用内存结果时，每个公式最终返回一个与完整 ResolvedExecutionDomain 对齐的 ndarray；OutputChunk 只是内部增量交付单位，不作为调用方需要自行拼接的公开结果。
- ComputeResult 直接携带任务实际使用的完整 ResolvedExecutionDomain；同一批内存数组共享这一顶层坐标契约。
- 编译、数据绑定和 Runtime 的可隔离失败按 LogicalPlan 依赖传播到 formula_id；独立输出可继续，任一必要分区失败则该公式整体未完成，约定内的 NaN/missing 不属于执行失败。
- 失败公式已经提交的全部内存分块和磁盘暂存分块必须按 formula_id 清理，不进入 ComputeResult.arrays 或 artifacts；其他独立公式不受影响。
- 磁盘输出按分块立即写入该公式的工作路径；任一必要分区失败时删除整个路径。某公式全部必要分区成功后立即按 formula_id finalize，但 finalize 不负责重新写入已有分块。
- OutputOptions 对整批公式统一启用内存、磁盘或两者，不在首版按 formula_id 选择结果目的地。
- ArtifactStore 管理可跨任务增量更新的长期因子数据集；Store 和数据集不绑定研究区间或固定资产成员轴，每个每日分区携带自己的 code 坐标并在读取时对齐到目标 ResolvedExecutionDomain。
- 任务内 formula_id 与长期 DatasetKey 分离；启用磁盘输出时，由独立 DiskOutputSpec 显式提供 formula_id 到 DatasetKey 的完整映射。
- 磁盘结果先完整写入公式 staging；计算或 staging 写入失败时删除工作区且正式数据不变。全部成功后逐日替换正式 code/data 文件；首版不保证 finalize 过程的跨日期原子性、崩溃回滚或并发读快照。
- 每日分区只保存至少一个 step 非 missing 的资产行；原任务轴内全 missing 与原任务轴外资产不作区分，读取到目标域时都补为 missing。
- 同一 DatasetKey 的跨任务写入必须通过规范化公式语义签名和数据规格校验；首版不提供 append/overwrite 模式，本次请求日期一律通过 staging 执行新增或替换。
- 第一版 Runtime 统一使用 float64 + NaN，Compiler 只区分 numeric、mask、code；DataProvider 规范化输入，输出适配器按目标格式转换。mask 必须保留 False 与 Missing 的区别。
- RuntimeReport 默认只做分区级轻量观测，不扫描数组或逐节点计时；详细算子 profiling 显式开启，取数、调度、写盘和结果质量不属于 RuntimeReport。

## 待决问题

1. `ExecutionDomain` 除资产、频率、日期和资产轴外，是否包含 delay、mask 和 point-in-time 策略？
2. 数据准备采用整批预取、按分区预取，还是由调度器按计划拉取？
3. 一批公式之间是否共享公共子表达式和叶子数据？

## 已完成的顶层议题

- 结果消费：已完成。已确定公式输出节点独立交付、完整内存数组、结果域、公式级失败清理与 finalize、整批输出目的地，以及固定结果组件边界。
- 存储结构：已完成。已确定长期数据集、每日自带 code 坐标的稀疏 NPY 分区、staging 后逐日替换、formula ID 到 DatasetKey 映射，以及语义签名校验和统一日期 upsert。

以下内容推迟到实现设计，不再作为顶层架构阻塞项：OutputChunk 的具体传输机制和数组所有权、内存与磁盘组件的局部设施故障、写入背压与进程间传输。
