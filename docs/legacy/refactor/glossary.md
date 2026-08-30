# 重构术语表

本文档用于消除重构讨论中的同词异义。未确定的术语会明确标为“待定”。

## 使用场景

### 交互式研究

研究者在一个已选定计算域的有状态会话中构造、试算、命名和物化因子。数据读取由系统代办，但研究体验不直接暴露底层数据源。

### 公式批任务

调用方一次提交一条或多条字符串公式，不要求预先注册 `FeatureDef`。任务仍需拥有一个明确的计算域和输出契约。

### 自动因子挖掘

高频地产生候选公式并调用公式批任务能力的上游算法。它额外需要便宜的预检、结构化诊断、资源限制和批内失败隔离。

## 核心模型

### 公式

用户或上游系统提交的 DSL 源文本。公式不是可执行计划，也不包含完整计算上下文。

### 研究 helper

研究平台提供的 Python 便利接口，用于构造 Surface AST 或 `FeatureDef`。研究 helper 不读取数据、不生成执行计划，也不由 Runtime 调用。`get_lf`、`get_hf`、`get_fund` 和 `FeatureDef.delay_lf` 属于这一层。

### DSL macro

公式语言中的表层结构，可以出现在字符串公式中，但不直接由 Runtime 执行。Compiler 会把它展开为更基础的显式表达式。如果一项便利能力需要同时供研究、批量计算和自动挖掘使用，应成为正式 DSL macro 或算子，而不是研究 helper 的隐藏行为。

### Runtime 算子

Canonical IR 中可实际执行的节点。算子拥有明确的输入、输出和执行契约，不访问研究定义、数据路由或物化存储。

### Surface AST

字符串公式经 Parser 解析，或研究 helper 直接构造出的表层表达式树。它保留调用方的表达方式，可能仍包含 DSL macro 和可自动展开的唯一投影。

所有入口必须先汇入同一种 Surface AST，才进入统一 Compiler。

### Compiler

把 `FormulaBatch` 编译为可执行逻辑计划的唯一门面。内部可以包含多个有顺序的 pass，包括名称绑定、macro 展开、空间推导、领域规范化、通用合法性检查、Canonical IR 生成、输入需求提取和 DAG 构造。

“多阶段 Compiler”不表示存在多套独立编译系统。

### 领域规范化（Domain Lowering）

Compiler 中把唯一的跨资产、跨频率投影等表层语义展开为显式 Canonical IR 节点的阶段。它不自动引入 delay 等 PIT 政策。

### 计算域（Execution Domain）

一次执行所处的坐标空间与时间语义。一个批次共享统一的资产类型、资产轴、输出频率、step 轴和输出日期区间。

输出日期区间表示调用方需要的结果范围。实际读取区间由计划根据 lookback 向前扩展。因此“每日增量更新”可以只请求一天的输出，但不等于所有输入都只读取当天。

是否还应包含 point-in-time、delay 和 mask 策略仍待决定。

### DomainSpec

调用方对计算域的简洁声明，可以包含日期区间、日历引用、资产类型、频率以及内联或命名资产轴。它尚未必包含精确的有序日期、代码和 step。

### ResolvedExecutionDomain

DomainSpec 在编译前解析得到的任务内不可变坐标契约，包含精确、有序的日期轴、资产轴和 step 轴。Compiler、PhysicalPlanner、DataProvider 请求和结果解释共同使用这一坐标契约。

### DomainResolver

把 DomainSpec 解析成 ResolvedExecutionDomain 的边界组件。纯内联轴可以在本地规范化；交易日历和命名资产轴通过 DomainCatalog 等外部元数据端口解析。DomainResolver 不读取公式叶子数值。

### 轴身份

判断两个日期轴、资产轴或 step 轴是否完全相同的依据。它可以由内联坐标、不可变引用、显式版本或内容 fingerprint/hash 提供。架构要求身份明确且可比较，但不要求调用方必须手工提供某一种 hash。

### ComputeRequest

三个入口共同使用的计算引擎请求。它只描述“算什么”，由一个统一 ExecutionDomain 和一组带任务内唯一 ID 的公式组成。

ComputeRequest 不包含 `FeatureDef`、alias 注册表、数据源位置、物化意图、chunk、worker 或内存预算。

### ExecutionOptions

描述“本次如何使用计算资源”的非语义参数，例如 worker 数、内存预算和执行后端偏好。同一个 ComputeRequest 使用不同 ExecutionOptions 必须保持计算语义不变。

### FormulaItem

ComputeRequest 中的一项公式，至少包含任务内唯一 `id` 和字符串或 Surface AST 表达式。`id` 用于关联输入、输出和诊断，不必等同于正式因子名称。

### 逻辑数据 Key

公式叶子引用的稳定逻辑标识，例如 `stk.1d.ClosePrice_Sus`。它描述“需要什么数据”，不直接描述数据库、表或文件位置。

### 数据路由

把逻辑数据 key 解析为具体数据源描述的过程。数据库、表、字段、parquet 路径等属于数据路由基础设施，不属于公式计算内核。

### 输入需求（Input Requirement）

编译计划显式列出的叶子数据需求，包括逻辑 key、所需计算域、lookback 和预期数据规格。它是计算层与数据准备层之间的候选协议。

### 数据绑定（Input Binding）

数据准备层针对某个输入需求提供给执行计划的实际数组或分区读取能力。绑定完成后，算子执行不再自行搜索 Store 或 Router。

### 投影（Projection）

把源空间的值映射到目标空间，并且每个目标坐标最多能由一个源坐标唯一确定。投影不需要 reducer，例如标准正股值投影到对应转债，或一个已选定的日频值广播到日内 step。

唯一且受注册规则约束的投影可以由编译期领域规范化自动展开，但必须在 Canonical IR 中成为显式节点。

### 聚合映射（Aggregation Mapping）

一个目标坐标对应多个源坐标，需要调用方声明 reducer 或 selector 的转换。例如多只转债映射回一只股票，或多个 1min step 聚合为一个 5min step。

聚合映射不能根据源域和目标域自动猜测，必须在公式中显式表达。

### Canonical IR

完成研究便利语义、唯一投影和其他表层语法展开后的规范化中间表示。所有 delay、mask、映射、广播和 resample 语义在进入 Runtime 前都必须显式存在于 Canonical IR。

### LogicalPlan

由 Canonical IR 构造的任务级、多输出逻辑 DAG。它表达节点依赖、输出归属、输入需求和语义属性，但不决定具体 chunk、worker 或数据加载方式。

### PhysicalPlan

LogicalPlan 结合执行域、资源策略和执行后端能力生成的物理执行方案。它决定分区、输入读取窗口、并发安排和结果提交顺序，但不得改变 Canonical IR 的语义。

### Engine Facade

计算引擎对调用方提供的公共门面。它接收一次完整计算请求，并协调 Compiler、PhysicalPlanner 和 Scheduler。它通过端口使用数据与结果设施，不直接实现数据库路由或物化存储。

### Scheduler

任务级执行协调组件。它按照 PhysicalPlan 请求分区数据绑定、调用 RuntimeBackend、控制并发与任务级资源，并把已就绪的 OutputChunk 交给启用的 MemoryResultAssembler 和/或 DiskResultWriter。它不修改公式语义。

### DataProvider

外部数据端口。它把已确定的逻辑输入请求解析、读取并对齐到叶子自己的 source space，返回 InputBindings。它不执行资产投影、频率投影或因子算子。

### ResultSink

早期用于统称内存收集、流式消费和事务性物化的外部结果端口。该通用概念已被 ADR 0020 取代。

首版 Scheduler 只协调固定的 MemoryResultAssembler 和 DiskResultWriter；任意 ResultConsumer、外部回调和引擎内结果评价不属于计算引擎。

### Runtime

执行已绑定输入的计划分区。它按 DAG 调用算子并管理分区内中间值，不编译公式、不取数、不决定 chunk，也不写物化存储。

### RuntimePartitionResult

原先用于表示 Runtime 执行一个已绑定计划分区后一次性返回的内部结果，包括 partition ID、共享的输出 Domain slice、按 formula ID 关联的输出数组以及独立的 RuntimeReport。

ADR 0014 已确定分区不是强制的结果交付原子单位：挂有 `formula_id` 的输出节点就绪后可以独立进入结果处理。`RuntimePartitionResult` 是否保留为分区完成、失败和报告协议，以及它是否继续携带数组，仍待后续设计。

### OutputChunk

某个挂有 formula ID 的输出节点在一个输出分区上的数组。其 shape 必须与该分区的 write dates、完整资产轴和完整 step 轴一致。

同一分区内的不同 OutputChunk 可以在各自输出节点就绪后独立进入结果处理。普通 DAG 中间节点不形成 OutputChunk。交付机制以及数组采用借用、复制或所有权转移仍待决定。

OutputChunk 是引擎内部的增量交付单位，不是启用内存结果时调用方需要自行拼接的公开返回格式。

### MemoryResultAssembler

启用内存结果时，接收各个公式的 OutputChunk，并将其装配成与完整 ResolvedExecutionDomain 对齐的单个 ndarray。完成后的数组进入 `ComputeResult.arrays[formula_id]`。

MemoryResultAssembler 不管理 DAG 中间值、不跨任务缓存、不计算 IC，也不负责持久化。它使用预分配、拼接还是其他缓冲实现仍待决定。公式失败时，它必须清理该 formula ID 已经装配的全部分块并释放所持数组引用。

### ComputeResult

引擎任务的最终结果对象。已确认其 `arrays[formula_id]` 在启用内存结果时是与完整 ResolvedExecutionDomain 对齐的单个 ndarray，而不是 OutputChunk 列表。

ComputeResult 直接携带本次任务实际使用的完整 `ResolvedExecutionDomain`。同一任务的所有结果数组共享这一顶层坐标契约，不在每个数组上重复携带轴。

失败公式不出现在 `arrays` 或 `artifacts` 中，其已提交分块全部清理，失败信息进入 `failures`。ComputeResult 的其他字段仍待后续决定。

### 公式级 finalize

某个 formula ID 的全部必要分区成功后，结果组件立即独立完成该公式的提交，不等待同一任务的其他公式。

对内存结果，finalize 表示完整数组已经封闭并等待任务结束进入 ComputeResult。

对磁盘结果，各 OutputChunk 在就绪后已经立即写入该公式的工作路径；finalize 只负责确认全部必要分块完整、写入完成标记或 metadata、注册或发布产物并产生 ArtifactRef，不重新写入分块。任一必要分区失败时，abort 删除该公式的整个工作路径。独立公式后续失败不回滚已经 finalize 的公式。

### OutputOptions

描述一次任务的结果目的地，不属于公式计算语义，也不进入 ComputeRequest。第一版对整批公式统一提供 `memory` 和 `disk` 开关，两者可以同时启用，不支持按 formula ID 分别选择目的地。

### DiskResultWriter

启用磁盘结果时接收 OutputChunk 的任务级组件。它按公式管理 begin、逐块立即写入、finalize 和 abort；任一必要分区失败时删除该公式的整个工作路径。

DiskResultWriter 决定何时接收和提交结果，不决定文件布局和编码。具体存储能力由 ArtifactStore 提供。

### ArtifactStore

磁盘结果的外部存储端口，负责文件布局、编码、读取、增量更新和原子发布能力。ArtifactStore 与 DiskResultWriter 分开设计：前者回答“怎样存”，后者回答“何时写、何时完成或清理”。

ArtifactStore 根路径不绑定研究区间或固定资产轴。它管理多个可跨任务增量更新的长期因子数据集。

### 因子数据集（Factor Dataset）

ArtifactStore 中由稳定 dataset key 标识的长期逻辑对象。它绑定因子身份、asset type、frequency/step 和值规格，但不绑定固定日期区间或永久资产成员轴。

因子数据集以日期为逻辑分区。每个日期分区携带自己的有序 code 轴和对应 values；load 时按这些坐标对齐到调用方请求的 ResolvedExecutionDomain。

每日分区只保存至少一个 step 非 missing 的资产行。原计算轴内结果全 missing 的资产与原计算轴外资产不作区分，读取到目标域时都表现为 missing。

数据集 metadata 还保存规范化公式 semantic signature。后续任务写入同一 DatasetKey 前必须校验公式语义和数据规格兼容，禁止不同公式静默混写。

### DatasetKey

ArtifactStore 中长期稳定的因子数据集标识。它可以来自 `asset.freq.name`、公司因子登记 ID 或上层命名空间，但不是调用方直接拼接的文件系统路径。

DatasetKey 与任务内 formula ID 是不同概念。启用磁盘输出时，DiskOutputSpec 显式提供 formula ID 到 DatasetKey 的映射。

### DiskOutputSpec

磁盘输出的任务级控制面，至少引用目标 ArtifactStore，并为任务中的每个 formula ID 提供 DatasetKey。它不进入 ComputeRequest，也不改变公式计算语义。

### 写入会话（Artifact Write Session）

某次任务为一个 formula ID 更新长期因子数据集时使用的临时生命周期。OutputChunk 持续写入会话工作路径；公式成功后提交相应日期分区，公式失败时删除整个工作路径。写入会话的清理不能破坏数据集中此前已经发布的历史分区。

首版先把全部结果完整写入 staging，再在 finalize 中逐日替换正式 code/data 文件。计算或 staging 写入失败时正式数据不变；但 finalize 过程不保证跨日期原子性、崩溃回滚或并发读快照。

写入会话不区分 append 和 overwrite 模式：本次请求中不存在的日期新增，已经存在的日期由 staged 文件替换，未涉及日期保持不变。

### RuntimeReport

与计算值分离的运行观测，例如节点耗时、峰值内存和算子诊断。它不属于因子语义。

第一版 minimal 模式只记录分区执行耗时、输入/输出字节数、可追踪峰值张量内存和节点计数，不扫描数组、不轮询进程 RSS，也不逐节点计时。详细算子统计通过 ExecutionOptions 显式开启。

### FormulaFailure

归属于一个或多个 formula ID 的结构化失败。失败可以发生在编译、输入绑定或 Runtime 节点执行阶段，并按 LogicalPlan 依赖关系传播。正常的 NaN 或 missing 数值不是 FormulaFailure。

### FailurePolicy

ExecutionOptions 中控制错误处理的非语义策略。`fail_fast` 在首个错误后停止；`continue_independent` 标记受影响输出并继续执行无依赖的输出。

### ValueKind

第一版 Compiler 使用的最小逻辑类型，取值为 `numeric`、`mask` 或 `code`。它描述张量的用途，不引入完整 NumPy dtype 提升系统。

### Runtime 值表示

第一版 Runtime 中所有计算张量统一使用 `np.float64`，所有 missing 统一为 `NaN`。

- mask 使用 `1.0=True`、`0.0=False`、`NaN=Missing`；
- code 使用有限整数值的 float64，`NaN=Missing`；
- 比较和逻辑算子必须保留 False 与 Missing 的区别；
- DataProvider 负责把外部 bool、integer、NULL 和 sentinel 规范化；
- 内存结果保持 Runtime 值表示；磁盘输出适配器和 ArtifactStore 负责按目标格式恢复 nullable bool、integer 或浮点类型。

### PIT 政策

决定某个数据值在计算时点是否可见，以及是否需要 delay 等处理的研究语义。引擎可以执行显式的 delay 或 as-of 算子，但不判断某个因子为了防止未来信息是否应当添加它们。

### 合法性检查

分为两种职责：

- **通用可执行性校验**：判断公式是否符合引擎公开的语言、算子契约、输入规格和统一计算域，属于引擎。
- **候选准入校验**：判断公式是否符合自动挖掘的搜索空间、复杂度偏好、业务意义、去重和研究约束，属于挖掘算法或其策略层。

引擎可以提供结构化诊断和算子元数据，但不内置挖掘策略。

### `FeatureDef`

研究平台中的持久化因子定义，包含命名、别名、公式、物化意图和研究策略。它不应成为公式编译与执行的必需输入。

### 研究平台

包裹计算引擎的有状态上层。它持有 `FeatureDef`、alias、helper、研究会话、物化记录以及对数据基础设施的引用，并把这些状态展开为完整的公式任务。

### 计算引擎

任务级、近似无状态的公式编译与执行能力。长期定义、数据路由、物化目录和跨任务缓存不作为引擎对象的隐藏状态。任务执行期间可以持有计划、输入绑定、中间值和任务级缓存。
