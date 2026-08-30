# 批量因子计算引擎设计决策记录

- 状态：草案
- 日期：2026-07-31
- 适用范围：基于远期重构架构的第一版批量因子计算引擎
- 配套文档：[architecture.md](architecture.md)

本文把当前讨论拆成可逐条确认的设计决定。状态含义如下：

- **已决定**：需求方已经明确，后续设计以此为约束；
- **建议接受**：结合当前实现、远期架构和 Zipline Pipeline 后给出的建议，尚待确认；
- **待决定**：存在多个合理方案，不能作为实现承诺。

本文是面向批量因子计算的落地提案，不取代 `core/docs/refactor/adr/`
中已经接受的远期 ADR。如果本提案最终改变远期 ADR，应另外新增或修订 ADR。

## 已决定

### DEC-001：第一阶段以字符串公式批量计算为主要场景

- 状态：已决定

第一阶段的主要输入是一批字符串公式，而不是必须预先注册的
`FeatureDef`。每条公式拥有任务内唯一的 `formula_id`，用于关联输出和错误。

交互式研究和长期物化能力可以继续存在，但不作为第一阶段计算内核的主导模型。

### DEC-002：字符串先解析为 Expr IR

- 状态：已决定

字符串公式先经过受限 DSL Parser，形成与当前 `Expr` 类似的表层 IR。Parser
只负责语法解析和 helper/macro 的表层展开，不读取数据、不执行算子。

多条公式必须先形成一个批次，再共同进入后续规划，以便构造任务级多输出图。

### DEC-003：资产和频率对齐在 Expr 阶段显式完成

- 状态：已决定

Parser 产出的 Expr 仍可能引用不同资产类型和频率的特征。类似当前
`Planner`，编译阶段负责：

- 推断节点所在的资产和频率空间；
- 把允许的资产映射、频率广播和重采样展开为显式算子；
- 拒绝不能唯一推导、且公式没有显式声明 selector 或 reducer 的转换；
- 生成只包含底层算子、常量和输入特征的规范化 Expr。

Term Runtime 不再临时猜测资产或频率对齐方式。

### DEC-004：规范化 Expr Lower 为任务级 Term DAG

- 状态：已决定

对齐后的 Expr 不再由递归 Executor 直接求值，而是 Lower 为任务级、多输出的
Term DAG。DAG 至少保存：

- Term 身份；
- 依赖边；
- 输出 `formula_id -> term_id`；
- 节点值规格和坐标规格；
- 窗口/lookback 需求；
- 可用于拓扑执行和生命周期管理的反向依赖信息。

Term DAG 是执行 IR；Expr 是公式语义和编译过程中的 IR。两者不承担同一职责。

### DEC-005：Term DAG 按拓扑序执行

- 状态：已决定

执行器对 DAG 做拓扑排序，按顺序处理 Term：

- 外部输入 Term 由 DataProvider 加载；
- 计算 Term 调用已注册的底层算子；
- 已在 workspace 中的 Term 直接复用；
- 最终只保留任务声明的目标公式数组。

第一版采用清晰的拓扑循环，不在递归 Executor 上叠加复杂生命周期管理。

### DEC-006：DataProvider 是计算阶段唯一的外部数据入口

- 状态：已决定

执行 Term DAG 时，外部特征统一通过 DataProvider 加载。当前
`DataRouter` 可以演化为 DataProvider 的第一版实现或适配器，但 Runtime
不再自行执行 Store、Router 和 runtime feature 的 fallback 搜索。

逻辑特征到表、字段、文件或内存对象的路由属于 DataProvider/数据目录能力。

### DEC-007：支持在公式入口通过 helper 声明外部输入

- 状态：已决定

除了直接引用稳定逻辑特征 key，字符串公式允许使用正式 DSL helper 声明一个
可加载输入，例如带数据源参数的基本面字段。

helper 必须在 Parser/Compiler 阶段展开为显式输入引用，最终 Lower 为外部输入
Term。Runtime 不调用 helper，也不读取 helper 的隐藏全局状态。

helper 的具体白名单、参数 schema、命名和序列化格式仍待实现设计。

### DEC-008：同一物理数据集的字段应批量读取

- 状态：已决定

多个外部输入来自同一个物理数据集，并且读取窗口与对齐要求兼容时，DataProvider
应一次读取多个字段，再分别转换为 Term 所需的数组。

“同资产、同频率”只是可合并的必要线索，不是充分条件。真正的批次键至少需要考虑：

- provider/backend；
- table、path 或 dataset identity；
- asset type 和 frequency；
- read dates、asset codes 和 step 轴；
- 影响查询语义的 reader 参数；
- 调整、缺失值和数据版本语义。

不能安全合并的输入回退到更小批次或单字段读取。

### DEC-009：使用 workspace 保存任务内 Term 值

- 状态：已决定

执行期使用任务或执行分区级的 workspace：

```text
workspace: term_id -> ndarray | scalar | provider value
remaining_consumers: term_id -> int
```

Term 完成后，其依赖的剩余消费者数递减；某个依赖不再被后续 Term 或最终输出使用时，
立即从 workspace 删除并释放引擎持有的引用。

输出 Term 需要额外保留一次引用，直到结果被收集或交给结果组件。

### DEC-010：计算的主要结果是完整域数组

- 状态：已决定

每个成功目标公式返回一个与任务目标 ResolvedExecutionDomain 对齐的数组。默认数组形状为：

```text
date x asset x step
```

同时提供根据 ResolvedExecutionDomain 坐标把数组转换为 DataFrame 的能力。DataFrame
不是 Runtime 内部值表示，也不进入算子执行路径。

### DEC-011：请求使用 DomainSpec 声明目标计算域

- 状态：已决定

第一版 DomainSpec 至少包含：

```text
DomainSpec
├── start
├── end
├── assets
├── target_asset
└── target_freq
```

`assets` 既支持目标资产的简写，也支持按资产类型声明选择范围的字典，例如为
`stk` 声明研究 universe、为 `idx` 指定一个或多个指数代码。

具体 Python/wire 类型尚待协议设计，但不应使用无约束的嵌套字典作为内部规范形式。

### DEC-012：DomainResolver 通过 DomainCatalog 解析精确坐标

- 状态：已决定

DomainResolver 把 DomainSpec 解析为任务内不可变的 ResolvedExecutionDomain。DomainCatalog
保存或提供：

- 交易日历和日期轴；
- universe 定义及版本；
- 各资产类型的有序 codes；
- frequency 对应的 step 轴；
- 轴身份或内容 fingerprint 所需的元数据。

ResolvedExecutionDomain 携带本次任务实际使用的精确坐标，编译、加载、执行和结果解释共享它。
DomainCatalog 不读取公式叶子的数值。

### DEC-013：输出域与输入读取域分离

- 状态：已决定

DomainSpec 的 `start/end` 描述调用方需要的输出日期。Term DAG 根据 rolling、
delay 等依赖推导 lookback；执行计划为各外部输入生成更大的读取日期窗口。

因此：

```text
output/write domain != input/read domain
```

每日增量计算只请求一天输出，也可能读取此前多日输入。

### DEC-014：SourceTerm 必须由输入语义显式声明

- 状态：已决定

本框架使用 `SourceTerm`，不沿用 Zipline 的 `LoadableTerm` 命名。

不把“DAG 中没有依赖其他 Term”作为 SourceTerm 的判定条件。

无依赖节点还可能是：

- 常量；
- 日期、代码或 step 轴；
- Runtime 内建值；
- 调用方预先放入 workspace 的值；
- 不需要输入的可计算算子。

反过来，外部输入 Term 也可能拥有加载约束。Zipline 中的 `LoadableTerm` 同样是显式
类型，而不是由图的入度推导。

Expr Lowering 根据输入节点的语义和绑定结果显式生成：

```text
LiteralTerm
SourceTerm
OperatorTerm
```

其中 SourceTerm 至少携带稳定逻辑 key、输入规格和 source space。DataRouter
在绑定阶段为它产生独立 SourceBinding，TermGraph 本身不被改写为物理数据节点。

外部输入必须由正式 DSL helper 构造为显式 `SourceExpr`/`SourceRefExpr`。这里的
“注册 SourceTerm”表示把外部输入声明放入当前 FormulaBatch 的 Expr 和输入需求中，
不表示 helper 修改 DataRouter 或进程级全局注册表。

例如 `get_lf("ClosePrice", if_adj=True)` 应展开为：

```text
multiply(
    source("stk.1d.ClosePrice"),
    source("stk.1d.adj_factor"),
)
```

Compiler 遍历这个 Expr 时会得到两个 SourceTerm。`if_sus=True` 同理会增加
`IfSuspended` SourceTerm 和显式 mask 算子。helper 负责构造语义，DataRouter/DataProvider
负责把这些 SourceTerm 绑定到实际数据源。

### DEC-015：Parser 与 Compiler 作为引擎内部子系统，由公共门面提供

- 状态：已决定

调用方可以直接提交字符串公式，由 Engine Facade 统一协调 Parser、对齐 Compiler
和 Term Lowering。这样所有批任务使用同一语言和编译契约。

为了调试和测试，可以额外公开 `parse()`、`compile()`、`explain()` 等专家接口，
但不要求普通调用方在引擎外先生成 Expr。

### DEC-016：DomainResolver 由引擎编排，DomainCatalog 作为外部端口注入

- 状态：已决定

Engine Facade 在编译前调用 DomainResolver；Resolver 属于引擎的任务编排能力，
其所依赖的 DomainCatalog 是外部基础设施端口。

这样既能保证 Compiler 总是得到固定坐标，又不会让计算内核持有日历、universe
数据库等长期状态。

### DEC-017：DataFrame 转换属于结果适配层

- 状态：已决定

数组到 DataFrame 的实现放在独立 `ResultFormatter`/`ResultAdapter` 中。
为了易用性，可以由 `ComputeResult.to_dataframe(...)` 委托给它。

该能力可以与引擎发布在同一个包中，但不属于 Compiler、TermGraph、Workspace
或 RuntimeBackend。这样可以避免 pandas 成为执行内核依赖，也避免不经意地把大型
三维数组全部复制成长表。

### DEC-018：使用 SourceTerm、OperatorTerm 和 LiteralTerm

- 状态：已决定

借鉴 Zipline 的执行算法，但不继承或复制其 `Term` 类层级。当前项目需要原生支持：

- 多资产类型和显式资产投影；
- 日频与日内 step 轴；
- 三维 `T x N x S` 数组；
- 参数化逻辑数据 key；
- 多公式批任务和公式级失败。

这些要求与 Zipline 主要面向二维 Pipeline 的模型不同。新 Term 协议围绕本项目的
ValueSpec、Domain 和 OperatorSpec 设计，第一版包含：

```text
Term
├── SourceTerm
├── OperatorTerm
└── LiteralTerm
```

- SourceTerm：外部数据输入，由 helper/SourceExpr 显式产生；
- OperatorTerm：调用 OperatorSpec，从其他 Term 的值计算；
- LiteralTerm：作为公式操作数的小型不可变字面量，例如 `1.0`、`NaN` 或编译器生成的
  小型索引数组。

`window=20`、`axis=0`、`periods=2` 等配置保留在 OperatorTerm 的 normalized params
中，不生成 LiteralTerm，因为它们决定算子语义、lookback 和结构签名。

大型用户数组不作为 LiteralTerm 内嵌，应通过 memory SourceTerm 或受验证的 initial
workspace 输入。日期、codes 和 step 等 Domain 坐标默认由 ExecutionContext 提供，
第一版不增加 DomainTerm。

### DEC-019：复用并演化 DataRouter 的数据字典

- 状态：已决定

不需要为了引入 DataProvider 重新建设一套数据目录。当前 DataRouter 已经提供：

- `data_dict` 字段目录；
- 完整逻辑 key 到 `SourceSpec` 的解析；
- 精确 source 配置、表级配置和内存 source 的优先级；
- 按字段、中文名和表搜索的能力。

建议第一阶段让 DataRouter 同时实现 SourceCatalog 和 DataProvider 协议：

```text
resolve_source(source_ref) -> SourceSpec
bind(source_term)           -> SourceBinding
search(query)               -> catalog rows
load_many(bindings)         -> term_id -> ndarray
```

其中 `data_dict` 继续作为字段发现和普通表字段绑定的依据。为支持可靠批量取数，需要逐步
补充稳定的 provider/dataset identity、物理表或路径、字段、参数和数据版本信息。

以后如果职责过重，可以在不改变 Compiler 和 TermGraph 协议的情况下，把目录查询拆为
SourceCatalog，把实际读取保留在 DataProvider。

### DEC-020：第一版使用任务级静态 lookback

- 状态：已决定

第一版不需要复刻 Zipline 为每个 Term 维护 `extra_rows`、offset 和不同输入窗口的精细模型。
采用一个更简单、保守的 session lookback：

```text
term_lookback(term) =
    local_lookback(operator, literal_params)
    + max(term_lookback(dependency))

job_lookback = max(term_lookback(output_term))
```

常见规则为：

- 叶子、常量、逐元素、截面和同日投影算子：`0`；
- `delay(periods=p, axis=date)`：`p`；
- 日序列 rolling window `w`：`w - 1`；
- 日内 step 轴上的 delay/rolling：日期 lookback 为 `0`；
- 多层时间算子按路径累加。

执行计划用 `job_lookback` 一次扩展整个任务或日期分区的 read dates，所有 SourceTerm
按这个统一窗口加载，最终输出再裁剪到 write dates。它可能比理论最小窗口多读一些数据，
但显著简化加载分组、workspace shape 和结果裁剪。

`local_lookback` 应成为 OperatorSpec 的声明式元数据或纯函数，不继续由 Planner
维护算子名硬编码集合。影响 lookback 的参数第一版必须是编译期可知的字面量，否则编译失败。

所有沿日期轴执行的算子第一版必须声明有限 lookback。`ffill(limit=None)`、累计窗口等
无界历史算子在日期分区模式下应拒绝，或要求调用方提供显式上限；不能假装为零回看。

DataProvider 为 as-of join、公告查询等进行的内部预取属于 source 实现细节。只要它最终
返回与 LoadRequest read dates 对齐的数组，就不需要把查询预取量传播为 DAG lookback。

如果 profiling 证明过量读取成为主要瓶颈，再演进为“按 LoadGroup lookback”，无需第一版
直接实现 Zipline 的逐 Term offset 模型。

### DEC-021：SourceTerm 与 SourceSpec 保持分离

- 状态：已决定

SourceTerm 是公式 DAG 中的语义节点，SourceSpec 是 DataRouter 解析出的物理数据源描述，
两者不应合并：

```text
SourceExpr / SourceRef
    ↓ TermLowering
SourceTerm
    ↓ DataRouter.bind()
SourceBinding
    ├── term_id
    ├── source_spec
    ├── read_domain
    └── load_group_key
```

SourceTerm 保存：

- `term_id`；
- 稳定逻辑 key；
- 影响数据语义的 source parameters；
- InputSpec/ValueSpec；
- source asset/frequency/domain。

SourceSpec 保存：

- provider/source identity；
- database/table/path；
- field/column；
- reader/query parameters；
- 物理 schema、缺失值和版本信息。

SourceBinding 属于 ExecutionPlan，负责把某个 SourceTerm、SourceSpec 和本次 read domain
关联起来。这样同一个 TermGraph 可以在不同环境绑定不同数据库或文件位置，物理路由变化
也不会改变公式语义签名。

现有 `SourceSpec` dataclass 可以继续作为第一版物理描述。新的 helper 最好产生
`SourceRef`，不直接内嵌 table/path；例如基本面 helper 携带 field、column_name、
quarters 等语义参数，由 DataRouter 决定实际 provider 和表。

### DEC-022：核心 API 使用 FormulaItem，文本由 Adapter 处理

- 状态：已决定

引擎核心 API 接收结构化 `FormulaItem[]`：

```text
FormulaItem
├── id
└── expression: str | SurfaceExpr
```

多行文本、Notebook 和 CLI 输入由 FormulaBatchAdapter 转换为 FormulaItem，不把行分隔、
注释和 ID 生成规则放入 Compiler。

字符串中的裸 dotted key 是 `source(key)` 的语法糖。SourceRef 只保存逻辑 key 和改变
数据产品语义的参数，不保存 raw/artifact/memory 等物理来源类型；DataRouter 根据环境和
路由策略生成 SourceSpec。

### DEC-023：ResolvedExecutionDomain 保存完整任务坐标

- 状态：已决定

ResolvedExecutionDomain 使用：

```text
ResolvedExecutionDomain
├── output_domain
│   ├── write_dates
│   ├── target_asset
│   ├── target_codes
│   ├── target_freq
│   └── target_steps
├── asset_axes: asset_type -> ordered codes
├── calendar/universe identities
└── fingerprint
```

它保存公式涉及的全部资产轴，使 Compiler 可以静态构造跨资产 OperatorTerm。动态
universe 使用区间 codes 的稳定并集作为固定轴；每日成员、上市和可交易状态通过 mask
SourceTerm 表达。

### DEC-024：Runtime 使用 float64、NaN 和三种 ValueKind

- 状态：已决定

本提案继承远期 ADR 0012：

```text
ValueKind = numeric | mask | code
physical dtype = float64
missing = NaN
mask = 1.0 / 0.0 / NaN
```

最小 ValueSpec 包含 `kind`、`domain_ref` 和 `physical_dtype`。DataRouter 负责规范化
外部值；比较、逻辑和 mask 算子必须保留 False 与 Missing 的区别。

### DEC-025：DataRouter 先 bind，再按组 load_many

- 状态：已决定

正式协议为：

```text
bind(SourceTerm[], ResolvedExecutionDomain)
    -> term_id -> SourceBinding

load_many(SourceBinding[], LoadRequest)
    -> term_id -> ArrayValue
```

全部 SourceTerm 在 Runtime 前完成 bind。执行期第一次访问某个 LoadGroup 时整组加载；
ArrayValue 必须严格满足 SourceBinding 的 read domain、shape、ValueSpec 和 missing 契约。
单字段读取保留为不能批量合并时的 fallback。

### DEC-026：Term 使用结构身份并执行最小 CSE

- 状态：已决定

结构身份为：

```text
LiteralTerm  = normalized literal + ValueSpec
SourceTerm   = SourceRef + InputSpec + source domain
OperatorTerm = operator semantic identity
             + dependency ids
             + normalized params
             + output ValueSpec
```

LiteralTerm 和 SourceTerm 自动去重；第一版 registry 只接受确定性、无副作用且不修改
输入的算子，因此结构相同的 OperatorTerm 也自动合并，`formula_id` 不参与 Term 身份。
未来允许有状态或修改输入的算子时，再引入执行 traits 并禁止这类节点自动合并。

### DEC-027：第一个可运行切片优先验证内核

- 状态：已决定

Slice 1 采用：

```text
whole-domain execution
memory output only
fail-fast
single-process
DataRouter 单字段读取 fallback
```

数据结构保留 FormulaFailure、LoadGroup、partition 和 ResultAdapter 扩展点。Slice 2
再加入 `load_many()` 的真实批量后端、日期 chunk 和独立公式继续执行。

实现状态（2026-07-31）：核心验证已完成 whole-domain/scope、memory-only、fail-fast、
single-process 执行，同时提前验证了多公式共同构图、Source bind/LoadGroup 和
`load_many()` 单字段 fallback。真实同表批量查询、日期 chunk 和独立公式继续执行仍未实现。

### DEC-028：对齐规则继承远期 Domain Lowering 决策

- 状态：已决定

第一版：

- 同资产、同频率直接对齐；
- 只自动 Lower 唯一资产/频率投影；
- 日频到日内只做结构广播，不自动添加 delay；
- 细频到粗频和多对一资产映射必须显式声明 reducer/selector；
- idx 必须显式选择后广播；
- delay、mask 和 PIT 政策由公式/helper 显式表达。

对齐规则属于独立 Domain Lowering/AlignmentRuleRegistry。它决定何时插入显式投影
OperatorTerm；OperatorSpec 只描述插入后那个明确算子的输入、输出和执行契约。

### DEC-029：OperatorSpec 使用五项轻量协议

- 状态：已决定

OperatorSpec 只保留：

```text
name
func
input_kinds
output_kind
date_lookback
```

- `func` 就是当前 `ops.py` 的算子函数；
- `input_kinds` 的固定 tuple 同时表达输入数量与 ValueKind；变长输入只使用一个
  `VariadicInput(kind, min_count)`；
- `output_kind` 声明固定 ValueKind 或沿用输入；
- `date_lookback` 是整数或读取字面量参数的纯函数，默认 `0`。

OperatorSpec 不负责 domain 推导。普通算子输出继承 Domain Lowering 后的公共输入
domain；显式投影、选择和重采样算子的目标 domain，由 AlignmentRule 在插入
OperatorTerm 时直接写入 Term 的 `domain_ref`。LiteralTerm 不参与 domain 选择。

第一版不定义 `semantic_version`、`InputContract`、`ParamSchema`、`pure` 或通用
`infer_output`。参数原样传给 `func`，只用函数签名排除明显的未知/缺失参数；不统一做
参数类型和范围校验。影响 lookback 的参数必须能在编译期求值。

第一版 registry 只允许确定性、无副作用且不修改输入的函数，因此不需要逐算子保存
`pure=True`。当前 `output_asset/output_freq/output_step/preserves_shape` 由
`output_kind + Domain Lowering` 取代。语义版本、持久化计划兼容性、完整参数验证和执行
traits 等有明确需求后再增加。

## 待决定

### OPEN-006：Slice 2 的日期分区和资源策略

Slice 1 已决定 whole-domain、single-process。后续仍需根据实际 workload 决定：

- 固定 date chunk 还是按内存预算自动生成；
- 分区间是否重新加载 SourceTerm；
- 输出装配与写盘背压；
- worker 并发和重试策略。

这些不阻塞内核验证。

### OPEN-009：公式批输入的文本协议

“多行字符串公式”还需要确定：

- 一行一条公式，还是允许公式跨行；
- `formula_id` 如何声明；
- 注释、空行和转义规则；
- 是否接受 `dict[formula_id, formula]` 作为更稳定的程序化协议。

核心 API 已经确定使用 FormulaItem[]，因此这里只影响 FormulaBatchAdapter。

### OPEN-010：Slice 2 的错误隔离实现

Slice 1 已决定 fail-fast。最终语义继承远期 ADR：按 Term DAG 依赖传播失败，独立公式
继续；任一必要分区失败时不返回该公式的部分数组。仍待决定的是 worker 崩溃、整组
数据加载失败和结果组件失败如何归因及重试。

### OPEN-011：初始 workspace 的正式用途

需要决定是否允许调用方注入：

- 已加载外部输入；
- 预计算中间 Term；
- 仅用于测试的固定数组；
- 跨任务缓存结果。

建议第一版只允许受验证的任务内 InputBinding 和测试注入，不把跨任务缓存作为
Engine 实例的隐藏状态。

### OPEN-014：LoadGroup 和数据失败细节

bind/load_many 主协议和加载时机已经确定，仍需在实现设计中定义：

- LoadGroupKey 字段；
- bind/load 错误模型；
- 同组字段的部分失败语义；
- 数据版本和 snapshot 一致性。

## 与当前实现的迁移关系

| 当前实现 | 新设计中的位置 | 主要变化 |
|---|---|---|
| `FormulaParser` | Parser | 从单公式扩展到 FormulaBatch，保留受限 DSL |
| `Expr` | Surface/Canonical Expr | 对齐后成为显式、可 Lower 的规范化 IR |
| `Planner` | Compiler passes | 移除数据读取与 Store 空间依赖，输出编译诊断和 Term DAG |
| `Executor._eval()` | TermExecutor | 从递归树求值改为拓扑循环 |
| `Executor._leaf_cache` | Workspace | 从叶子缓存扩展为全部 Term 生命周期管理 |
| `DataRouter` | DataProvider/SourceCatalog 适配器 | 增加 `load_many`，不再由 Runtime fallback 搜索 |
| `FeatureStore.resolve_space()` | DomainResolver/DomainCatalog | 任务坐标显式解析，不从 snapshot 隐式获得 |
| `FeatureManager` chunk 循环 | PhysicalPlan/Scheduler | 分区进入引擎任务执行子系统 |
| `CalculationResult` | ComputeResult | 支持多公式数组，共享 ResolvedExecutionDomain |

## Zipline 借鉴边界

本提案借鉴 Zipline Pipeline 的以下机制：

- Zipline 的显式 `LoadableTerm` 与 `ComputableTerm`，在本项目中对应 SourceTerm
  与 OperatorTerm；
- Term 依赖图和拓扑执行；
- 按 loader 与兼容读取窗口批量加载列；
- workspace 保存 Term 值；
- 根据依赖引用计数及时释放 workspace 数组；
- 最终将数组和 domain 坐标转换为 DataFrame。

不直接照搬：

- Zipline 的具体 Term 继承体系；
- `AssetExists` 和美国股票 AssetFinder 语义；
- 主要面向二维 `date x asset` 的数组约束；
- AdjustedArray、window iterator 和 mask 的具体实现；
- Pipeline screen 决定最终窄表行的 API。

参考：

- [Zipline Pipeline Engine 源码](https://zipline.ml4trading.io/_modules/zipline/pipeline/engine.html)
- [Zipline Term 源码](https://zipline.ml4trading.io/_modules/zipline/pipeline/term.html)
- [Zipline Reloaded TermGraph 源码](https://github.com/stefan-jansen/zipline-reloaded/blob/main/src/zipline/pipeline/graph.py)
