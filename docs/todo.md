# TODO

本文用于记录当前项目已经明确需要推进、但尚未完成的功能项。Bug 请记录在 [`known_bugs.md`](known_bugs.md)，代码理解和设计答疑请记录在其他 review 文档中。

## 1. 支持直接使用 Python helper 定义 `common_inputs`

### 目标

提供易用的结构化 API，使调用方可以直接使用 `source()`、`get_lf()`、`get_hf()`、`get_fund()`、`load_factor()` 和 `operator()` 等 Python helper 定义 `common_inputs`，而不必把它们写成字符串。

期望使用方式示例：

```python
batch = FormulaBatch.from_exprs(
    common_inputs={
        "close": get_lf("stk", "ClosePrice"),
        "volume": get_lf("stk", "TurnoverVolume"),
    },
    formulas={
        "alpha": operator(
            "divide",
            operator("ts_mean", ref("close"), window=5),
            ref("volume"),
        ),
    },
)
```

具体 API 名称和是否需要 `ref()` 仍需设计，上述代码只表达目标体验。

### 当前状态

底层已经支持直接构造 `Binding`、`FormulaProgram`、`FormulaBatch` 和不可变 Expr AST，但缺少面向调用方的简洁 Builder/Adapter。直接使用底层 dataclass 需要手工创建 `Binding` 和 `SymbolRefExpr`，可读性和易用性不足。

### 实现要点

- 新入口最终仍应生成现有 `FormulaBatch + FormulaProgram + Expr`，不建立第二套编译语义；
- 保留与字符串入口相同的名称绑定、作用域、保留名称和 CSE 规则；
- 明确 common input 和公式局部 binding 的顺序表达方式；
- helper 调用不得读取数据、访问全局 Registry 或产生副作用；
- 错误信息应能定位到 common input/formula/binding 名称；Python 构造入口没有源码行列时允许无 `SourceSpan`；
- 支持 helper 的全部语义参数，例如复权、PIT、quarters 和 source 参数；
- 文档同时给出字符串入口与 Python Builder 入口示例。

### 验收标准

- 无需任何公式字符串即可构造并执行一个多公式 `FormulaBatch`；
- 同一公式分别通过字符串和 Python Builder 构造时，产生相同的 LogicalPlan 语义身份；
- 支持 common input、顺序局部 binding 和最终输出；
- 前向引用、跨公式引用、重复定义和 common input 覆盖仍被拒绝；
- helper 参数可以完整进入 `SourceRefExpr.semantic_params`；
- 为直接 Python 入口补充独立单元测试和 README 示例。

## 2. 明确指数成分内统计与行业分类统计的 helper/operator 设计

### 目标

统一设计两类容易混淆的业务统计入口：

- 指数成分内统计：按某个指数的成员 mask 做截面 reduce 或成员内 transform；
- 行业分类统计：按行业代码分组做 reduce、demean 或 zscore。

helper 只负责把指数、行业标准、层级等业务参数转换为稳定 Source 和普通
OperatorExpr；底层 operator 只处理数组、ValueKind 和 Domain，不感知指数代码、行业标准
或物理字段。

候选使用方式示例：

```python
index_stat(
    value,
    index="CSI300",
    method="mean",
    sample_mask=tradable,
    weight=index_weight,
)

industry_stat(
    value,
    standard="SW2021",
    level=1,
    method="demean",
    sample_mask=tradable,
)
```

具体名称只是候选。需要明确指数 helper 是接收稳定指数标识并自动注入成员 Source，还是
继续要求调用方显式传入 `member` 表达式；两种入口不能形成重复语义。

### 当前状态

当前已有：

- `index_member_stat(values, member, method=...)` helper，可展开为
  `member_mean/member_sum/member_std`；
- `member_demean/member_zscore` 等成员内 transform operator；
- `group_mean/group_sum/group_std/group_demean/group_zscore` 等行业可复用 operator；
- 指数成员、指数权重和行业代码 Source 的 DataProvider 支持。

尚未形成统一且明确的公开契约：指数 helper 仍要求用户提供 member 表达式，行业没有新
pipeline 专用 helper；reduce 与 transform 的输出 Domain、mask/weight 支持范围和命名也
没有集中说明。legacy 中已有的业务 helper 不能直接视为新 pipeline 设计。

### 实现要点

- 先列出指数与行业各自支持的 method，并区分资产轴 reduce 与保持完整资产轴的 transform；
- 指数 helper 应规范化为成员/权重 `SourceRefExpr` 加现有 `member_*` OperatorExpr，不复制数值 kernel；
- 行业 helper 应规范化为行业代码 `SourceRefExpr` 加现有 `group_*` OperatorExpr，不复制数值 kernel；
- 明确指数标识、行业标准和层级的稳定命名，例如 `CSI300`、`SW2021/L1`，并使其进入 semantic identity；
- DataProvider 自动为成员 mask、指数权重和行业代码声明正确的 `ValueKind.MASK/NUMERIC/CODE`；
- 明确 missing member、行业缺失、未覆盖资产、sample mask、权重和 NaN 的组合语义；
- 明确股票与可转债的支持范围，以及转债按自身分类还是正股映射后的分类统计；
- helper 自动注入的 Source 必须经过普通 Source 收集、描述和加载链路，不能在 Compiler 或 kernel 中读取业务数据；
- 底层 `member_*` 与 `group_*` 是否共享内部 kernel 可以实现时决定，但公开契约不应因代码复用而混为一类。

### 验收标准

- 指数成分内 mean/sum/std 与 demean/zscore 的输入、输出 Domain 和广播行为均有明确契约；
- 行业 mean/sum/std 与 demean/zscore 的输入、输出 Domain 和广播行为均有明确契约；
- helper 展开后不残留 `HelperExpr`，并只产生普通 SourceTerm 与已注册 OperatorTerm；
- 成员 mask、指数权重和行业代码自动获得正确 ValueKind；
- 不同指数、行业标准和层级产生不同语义身份；
- 覆盖缺失分类、非成员、sample mask、权重、日频 singleton 和日内广播场景；
- README 和调用链文档分别给出指数与行业的最小示例。

## 3. 实现真正的批量 `load_many()`

### 目标

让同一 LoadGroup 中来自相同物理数据集、具有兼容查询语义和相同 ReadDomain 的多个字段，通过一次真实物理 I/O 加载，而不只是一次 API 调用内部逐字段循环。

### 当前状态（已实现）

`SmartQuantDataProvider` 已按 `load_group_key` 对日频宽表执行多字段单次 SQL，
对分钟 parquet 执行多字段单次 DuckDB scan，并共享日期过滤、代码映射和 step
对齐。每次物理 I/O 记录字段、范围、行数、字节、耗时和 batch 模式；组内错误
fail-fast。旧 `FeatureStoreDataProvider` 兼容层已于 2026-08-18 移除，旧数据设施
归入 `factor_engine.legacy`，见 [`取数链路演进归档.md`](取数链路演进归档.md)。
实现由独立 Catalog、公共 Backend、dataset 批量模板和统一 Normalizer 组成，未在
Provider facade 内复制旧 Router/Reader 链。
公司 HPC 路径的性能基线属于部署环境验证，尚未写入仓库。

### 实现要点

- 为 Reader 增加数据集级批量接口，一次查询/扫描多个字段；
- 同组字段共享日期过滤、代码映射、step 对齐和数据库连接；
- 查询结果必须稳定映射回每个 `SourceBinding.term_id`；
- 同组字段允许不同 `field/name`，但物理表、参数和 ReadDomain 必须兼容；
- 对无法批量读取的 Source 保留显式 fallback，并记录诊断信息；
- 避免 Router cache 为不同分区 scope 无界累积；
- 增加批量字段的 dtype、missing、ValueKind 和 shape 规范化；
- 统计逻辑 load call、物理查询次数、读取字节和耗时。

### 验收标准

- 同表 close/volume 等字段只产生一次 SQL 或一次 parquet scan；
- 物理查询次数通过测试 double 或 Reader diagnostics 可验证；
- 批量结果与逐字段 fallback 数值完全一致；
- 任一必要字段失败时整个 LoadGroup fail-fast；
- 不同表、不同 ReadDomain 或不兼容参数不会错误合组；
- chunked 和 whole-domain 执行结果一致；
- 有针对真实宽表/分钟数据的性能基准。

## 4. 实现独立的新 DataProvider

### 目标

实现面向新 batch engine 契约的正式 DataProvider，而不是主要通过 `FeatureStoreDataProvider` 适配旧 Store、Router 和 SmartQuant Reader。

新 Provider 应原生实现：

```text
calendar_dates()
asset_codes()
describe_many()
bind_many()
load_many()
```

并明确承担 Source catalog、静态输入契约、物理 source binding、批量加载和 Runtime 数据规范化职责。

### 当前状态（已实现）

已新增任务级 `SmartQuantDataProvider`，原生实现五个 Provider 方法。它每任务解析
catalog，只发布真实存在物理表的 84 个基本面 ItemCode；使用 `SecuMarket=83` 的
交易日历，并按完整任务 read horizon 从股票、转债和指数行情表冻结 `InnerCode`
资产轴。日频、分钟、基本面、复权、不可交易、行业、指数成分和转债正股关系均
直接规范化为 `float64 T×N×S`。已保存因子继续由 `RepositoryDataProvider` 组合，
不混入基础 Provider。实现不依赖 `legacy`。
正式读取链同时不依赖旧 Store、DataRouter、SmartQuantSourceReader 或 FeatureArray；
旧数据层只保留 Snapshot/legacy 兼容入口，并复用中性的 OceanBase backend。

### 实现要点

- 先确定 Source Catalog：logical key、InputSpec、SourceSpec 和 ValueKind 的权威来源；
- `describe_many()` 必须只读取元数据，不触发大规模数据加载；
- `bind_many()` 根据 SourceTerm 和当前 ReadDomain 生成精确且稳定的 SourceBinding；
- `load_many()` 原生支持同数据集多字段批量读取；
- 所有返回值规范化为精确 `float64 T × N × S` 和 `NaN` missing 协议；
- 正确处理股票、可转债、指数、日频、分钟频率、基本面和已保存因子；
- 明确 calendar 和 asset master axis 的快照身份与更新策略；
- 明确缓存的任务生命周期、容量和失效规则；
- 提供结构化诊断：物理查询、命中缓存、读取范围、耗时、字节数和错误 source；
- 不在 Provider 中执行资产投影、频率聚合或因子 operator；这些仍属于 Compiler/Runtime；
- 与正式 FactorRepository 的边界保持独立。

### 验收标准

- 新 Provider 不依赖旧研究层 `factor_engine.legacy`；
- 编译阶段可以稳定完成 Source 描述和 Domain 解析；
- 物理 Source 位置变化不改变等价 LogicalPlan 的 semantic identity；
- 真实后端通过完整 DataProvider 契约测试；
- chunked/whole-domain、lookback、多 Source load group 和跨资产输入均有集成测试；
- 错误 dtype、shape、日期顺序、代码缺失和不完整 load group 均能明确失败；
- 迁移路径：兼容层已移除，旧 Snapshot 链路的演进与迁移背景记录在
  [`取数链路演进归档.md`](取数链路演进归档.md)。

## 5. 补全新 batch pipeline 的算子能力

### 目标

建立新 batch pipeline 的权威算子能力清单，补齐公式语法已经表达或常用因子计算需要、但默认 Operator Registry 尚未提供的 operator，并确保字符串入口与 Python Builder 入口能力一致。

至少应先补齐幂运算。Parser 当前会把：

```python
factor = x ** 2
```

转换为 `OperatorExpr("power", ...)`，但默认 Registry 没有注册 `power`，因此合法语法会在 Compiler 阶段报 `Unknown operator 'power'`。

### 当前状态

- 新引擎已有 elementwise、timeseries、cross-section 和 alignment 的一批数值 kernel；
- Parser、Registry、Compiler Domain Lowering 和文档中的算子集合尚未形成一个权威清单；
- 部分语法能够生成尚未注册的 operator，例如 `power`；
- 部分已注册 operator 又缺少正确的 Domain Lowering，该问题单独记录在 `known_bugs.md` 第 5 项；
- 旧研究层存在的 operator/helper 不能视为新 batch pipeline 已支持。

本 TODO 负责“应该提供但尚未提供的算子能力”；已经注册但违反 Domain/shape 契约的算子仍按已知 Bug 修复。

### 实现要点

- 建立 Parser 语法、Python `operator()`、默认 Registry、Compiler lowering 和文档之间的能力矩阵；
- 优先补充 `power`，并明确 NaN、零、负底数和非整数指数语义；
- 盘点常用逐元素、时序、截面、step 和资产对齐算子，明确当前版本支持范围；
- 新增 operator 必须声明 Tensor Inputs、Literal Params、ValueKind、date lookback 和输出 Domain；
- shape-changing operator 必须先具备显式 Domain Lowering，不能只注册 kernel；
- 不准备支持的语法应在 Parser 或 Compiler 阶段给出明确错误，不能延迟到 Runtime；
- 字符串表达式和 Python AST Builder 构造的等价调用应产生相同 semantic identity；
- 为默认 Registry 建立逐算子的契约测试，而不只测试少数主流程示例。

### 验收标准

- `x ** 2` 和显式 `power(x, 2)` 可以编译、执行并产生相同结果与语义身份；
- 默认 Registry 中每个 operator 都有输入数量、ValueKind、参数、lookback、Domain 和 Runtime shape 测试；
- Parser 能生成的内置 operator 都已注册，或在语法层被明确拒绝；
- 文档列出的 operator 均能通过新 batch pipeline 执行；
- whole-domain 与 chunked 执行结果一致；
- 未支持能力在数据加载前 fail-fast，并包含 operator 名称和公式位置。

## 6. 支持受控的负 periods 与 future read horizon

### 目标

在明确建模未来数据依赖、分区读取范围和使用策略后，支持 lead/负 periods，而不是让负 `delay` 在现有历史 lookback 模型中直接执行。

### 当前状态

当前 PhysicalPlanner 只支持：

```text
read_dates = write_dates + 之前最多 job_lookback 个交易 session
```

`delay/step_delay/step_diff/step_pct_change` 因此统一要求 `periods` 为非负整数。负 periods 会在编译期失败，Runtime 不会读取未来分区。

### 实现要点

- 为 Term 和 LogicalPlan 增加明确的 future horizon，不能把它编码成负 lookback；
- PhysicalPlanner 同时计算历史 lookback 和未来 read dates，并正确处理任务尾部日历不足；
- whole-domain 与任意 `chunk_size` 必须产生一致结果；
- 明确负 periods 是继续作为 `delay(..., periods=-n)` 暴露，还是增加语义更清晰的 `lead()`；
- 日期轴和日内 step 轴的未来依赖都必须纳入契约，不能只处理跨日期读取；
- 明确研究、回测和生产场景是否允许未来依赖，并提供可审计的计划诊断；
- future horizon 必须进入 LogicalPlan semantic identity 和执行诊断；
- 不允许通过 ExecutionOptions 静默改变是否可使用未来数据；
- 错误信息应区分“不支持 future read”“策略禁止 future read”和“日历未来数据不足”。

### 验收标准

- 日期轴负 periods 在显式允许的场景中按 future horizon 正确执行；
- step 轴负 periods 的日内未来依赖具有同样明确的策略；
- whole-domain、不同 chunk_size 和分区边界结果一致；
- 任务末尾缺少未来日期时按契约输出 Missing，不读取请求外的未授权数据；
- LogicalPlan 和诊断能够展示每个输出需要的 past/future horizon；
- 默认策略仍拒绝未来依赖，避免因公式参数意外引入前视数据；
- 有未来泄漏防护和策略审计测试。

## 7. 实现基于任务 Domain 的安全资产位置映射

### 目标

提供 `project_stk_to_cb(values)`，由 helper 自动注册 mapping Source，并保持 operator
不感知股票、转债和 InnerCode 业务语义。DataProvider 把业务资产关系转换为相对于
本次任务股票 Domain 的列位置，再交给通用 `lookup_by_col()` 执行。

### 当前状态（已实现）

helper 会在 Source 描述前自动注入 `cb.1d.underlying_stk_col`，并 Lower 为
`lookup_by_col(values, mapping)`。正式 `SmartQuantDataProvider` 是任务级实例；编译期
冻结 `stk/cb` 有序代码轴，`CBStockMap` loader 读取 `CB -> 正股 InnerCode` 后使用该
实例的 `stk.codes` 生成转债轴位置。正股不在任务股票子集时输出 NaN。

该任务级位置 Source 只有正式 `SmartQuantDataProvider` 支持；已移除的
`FeatureStoreDataProvider` 兼容层曾明确拒绝它，避免恢复基于 Store master axis
的不安全路径。通用 `lookup_by_col()` kernel 和 Registry 没有股票、转债
或 InnerCode 分支。

### 实现要点

- helper 自动注入 mapping Source，用户不需要声明或传入 mapping；
- 每个正式 Provider 实例只服务一个任务，并冻结任务有序代码轴；
- mapping 在 `load_many()` 的 `CBStockMap` loader 内根据本实例 `stk.codes` 生成；
- 位置 mapping 不进入跨任务缓存；
- 正股未包含在当前股票 Domain 时使用 NaN；
- 业务关系留在 DataProvider，`lookup_by_col()` 只执行数组位置 gather。

### 验收标准

- 完整股票轴、显式子集和任意代码重排均能正确完成转债正股投影；
- 股票数与转债数相同但轴身份不同的场景不会静默错算；
- mapping 列位置只根据本次任务冻结的 Domain 生成；
- whole-domain 与不同 `chunk_size` 的结果一致；
- operator kernel 和 Registry 中不出现股票、转债或 InnerCode 业务分支；
- helper 不要求用户显式声明 mapping，旧 Provider 的不安全路径保持禁用。

## 8. 算子性能优化：减少数组复制并使用 Numba 加速

### 目标

系统梳理现有算子的内存分配和执行热点，在不改变数值、缺失值、Domain 与广播语义的前提下，优先使用 NumPy view、singleton 广播和预分配输出，避免不必要的大数组复制，并将适合的循环型 kernel 迁移到 Numba。

### 实现要点

- 盘点 `astype()`、`copy()`、`np.broadcast_to(...).copy()` 和中间布尔数组等潜在复制点；
- 广播结果只读时优先保留 NumPy view，仅在算子确实需要原地写入时复制；
- 截面、时序、分组和重采样等循环密集路径优先评估 Numba kernel；
- Numba 边界保持纯数组和基础标量参数，不把 Domain 或业务对象传入 kernel；
- 避免为了加速而物化 `T × N × S` 中间数组，reduce 应直接生成目标形状；
- 为优化前后增加内存峰值、执行耗时和数值一致性基准。

### 验收标准

- 优化前后算子数值、NaN、三态 mask、shape 和 Domain 行为完全一致；
- 主要热点算子不再产生可避免的广播数组复制；
- 适合 JIT 的循环型算子具有缓存启用的 Numba 实现；
- whole-domain 与不同 `chunk_size` 的结果一致；
- 性能基准能够展示耗时和峰值内存的实际改善。

## 9. 将 `align_frequency` 算子化或统一复用 `resample`

### 状态

已完成（2026-08-12）：选择独立公开 `align_frequency` 算子，保留与 `resample` 分离的方向语义。

### 目标

消除当前 `align_frequency` 由 helper 展开为私有 operator 的双层命名，
使显式频率转换使用一致的公开算子模型。需要在实现前确认以下两种最小方案之一：

1. 将 `align_frequency(expr, target_freq, method="ffill")` 注册为普通公开算子，并保留其专属 Domain lowering；
2. 扩展公开 `resample` 的方法契约，使粗到细的 `ffill` 与细到粗的统计聚合统一由一个频率转换算子表达。

无论选择哪种方案，都不允许恢复隐式频率转换。

### 修复前状态

- `resample` 是默认 Operator Registry 中的公开算子，细到粗转换由 `_lower_resample()` 推导 Domain；
- `align_frequency` 属于 `DEFAULT_HELPERS`，Compiler 将其改写为私有 operator；
- Engine 初始化时单独追加对应的 Runtime OperatorSpec；
- 两者都依赖标准 step 轴和显式目标频率，但公开模型、注册位置和命名方式不一致。

### 设计约束

- 粗频率到细频率只允许明确的填充/映射方法，首版仍可只支持 `ffill`；
- 细频率到粗频率必须明确 reducer，不能用 `ffill` 代替统计聚合；
- 转换只改变 frequency 和 step_count，保留 asset、codes、calendar 和 axis identity；
- DataProvider 始终加载原始频率 Source，不执行对齐或重采样；
- 字符串公式、Python `operator()` 和 `get_hf` 语法糖必须落到同一套公开算子契约；
- 如果复用 `resample` 会让参数、方向或 method 语义变得含糊，应选择独立公开
  `align_frequency` 算子，不为了减少一个名字而混合两个概念；
- 不保留旧私有名称的兼容别名或迁移路径。

### 实现要点

- 对比两种方案的公开参数、Registry 契约、Domain lowering 和 Runtime kernel，选择更简单且语义清晰的一种；
- 删除 Parser helper 与私有 Runtime OperatorSpec 的重复注册路径；
- 复用现有 `get_ffill_step_index()` 和完整标准 step 轴校验，不重新实现时间坐标规则；
- 非法方向、非整数频率关系、不完整 step 轴和不支持的 method 必须在加载前失败；
- 更新频率对齐文档、调用链、README 和计划可视化输出。

### 验收标准

- 用户只通过公开算子表达粗到细频率对齐；
- LogicalPlan 中只出现公开 `align_frequency`；
- 公开算子在默认 Registry 中具有唯一 OperatorSpec 和唯一 Runtime kernel；
- 粗到细 `ffill`、细到粗 reducer 和日频相关边界均有独立测试；
- 非法转换在 DataProvider 加载前失败；
- whole-domain 与不同 `chunk_size` 的结果一致；
- 仓库代码和非 legacy 文档中没有旧私有调用方式残留。

## 10. 减少分钟加载的日期证券代码映射查询

### 当前状态（股票已完成）

股票分钟 loader 已改读静态全集表 `SmartQuant.InnerCode_SecuCode`，只按当前
`ReadDomain.codes` 过滤，不再从 `ReturnDaily` 按日期重复读取映射。该表的
`SecuCode` 唯一，允许一个 `InnerCode` 对应多个历史代码；最终坐标 duplicate 检查
仍然保留。

可转债不在该表中，仍按当前 PhysicalPartition 从 `CBReturnDaily` 读取日期相关映射。

### 背景

分钟 parquet 没有 Runtime 资产轴使用的 `InnerCode`，只有 `security_code`，因此分钟
loader 必须通过权威代码表获得映射；映射可能是静态全集，也可能随日期变化：

```text
(DataDate, InnerCode, SecuCode)
→ (date_idx, asset_idx, SecuCode)
```

同一 minute LoadGroup 的多个字段共享映射和一次 parquet scan；分区完成后不保留
跨 partition cache。

### 后续评估

- 优先评估分钟 parquet 上游直接写入稳定 `InnerCode`，从根源上删除运行时映射查询；
- 为可转债提供等价的权威静态 `InnerCode/SecuCode` 映射后，再删除日期映射查询；
- 只有确认收益后才考虑任务生命周期内、容量明确的只读映射复用，并明确失效和内存上限；
- 任何方案都不得扩大单次 parquet read domain、改变 `chunk_size` 语义或形成跨任务缓存；
- 保留日期维度，不能假设 `SecuCode` 与 `InnerCode` 的关系永久不变。

## 11. 补齐 Domain 校验路径的测试覆盖

### 目标

Domain 体系的核心承诺是"非法域在编译期响亮报错、绝不静默错位"，但大量校验分支目前没有任何测试断言，该安全机制不受测试保护。需要先补刻画测试，再谈对该层的任何重构。

### 当前状态

- `Compiler._resolve_domain()` 的全部校验分支（target_asset 不在 asset_scope、多日历冲突、日期区间为空、asset_scope 未覆盖输入资产、非法 selector、显式子集为空、显式子集重复、未知资产代码）零测试断言；
- `operators/domain_rules.py` 仅 "incompatible frequencies" 与 "align it explicitly" 被 `tests/test_alignment_rules.py` 覆盖；"must share the same full asset axis"、"Incompatible step dimensions"、"cannot select the partitioned date axis"、"empty step axis"、位置越界等分支均未覆盖；
- `_validate_output_domain()` 仅资产轴不匹配一个分支被覆盖（`tests/test_batch_engine.py`），标量输出、日历不匹配、频率不兼容、step 数不可广播均未覆盖。

### 实现要点

- 用 `MemoryDataProvider` 构造最小用例逐分支锁定错误类型与消息，不依赖真实后端；
- 重点覆盖 domain_rules 的广播边界：singleton 广播、两个不同名满轴混合、1d singleton 并入日内的豁免与其失效条件、step 数不兼容；
- 覆盖 `_resolve_domain()` 的 lookback 外扩边界（回看超过日历起点时的 `max(0, ...)` 钳制）与显式代码子集的成功路径；
- 补测试过程中如发现错误消息表述不一致，先记录、不顺手改语义；
- 测试就位后，再评估是否收敛错误消息为公开契约。

### 验收标准

- 上述每个编译期 Domain 校验分支至少有一条断言错误类型与消息的测试；
- domain_rules 的每类广播/拒绝规则都有正例与反例；
- 全量测试与 ruff 通过，且新增测试能捕获对校验逻辑的回归修改。

## 12. 收敛 TermDomain 与 ResolvedOutputDomain 的镜像关系

### 目标

明确"任务输出域"与"Term 域"两个概念的边界，消除手工逐字段复制，降低 Domain 类型族的学习和维护成本。

### 当前状态

- `TermDomain` 与 `ResolvedOutputDomain` 各自携带 asset_type、codes、frequency、calendar、指纹五元组，`_term_domain()`（`compiler.py`）在两者之间手工逐字段复制；
- `ResolvedOutputDomain` 额外持有完整 dates 与 steps 值（结果装配需要）；`TermDomain` 只记录 step_count，并参与 semantic key 哈希；
- `TermDomain` 必须保持 frozen 且可哈希；`ResolvedOutputDomain` 含 NumPy 数组，不可哈希。二者确有真实差异，不能简单合并为一个类型。

### 实现要点

- 先评估两种收敛深度：仅把 `_term_domain()` 改为 `ResolvedOutputDomain` 上的构造方法消除手工复制，或让 `ResolvedOutputDomain` 内嵌 `TermDomain`；
- 保持 `TermDomain` 的不可变与可哈希性，NumPy 数组不得进入 semantic key；
- 不改变 semantic key 中域身份的构成语义；如调整哈希输入结构，须同步说明对 semantic identity 的影响；
- 同步更新 `CONTEXT.md` 的 Domain 词条与 `docs/资产轴对齐规则.md` 的相关表述。

### 验收标准

- `_term_domain()` 的手工字段复制被消除；
- 两个类型的职责差异（任务输出域 vs Term 身份）在类型定义处有明确说明；
- semantic identity 行为不变，全量测试通过。

## 13. 类型化 Catalog 配置管道并统一 SQL 谓词构造

### 目标

消除取数层的字符串索引 dict 管道与重复 SQL 过滤构造，使 Catalog 到 loader 的链路可静态检查，降低拼写错误只能在运行时暴露的风险。

### 当前状态

- `Catalog` 的 dataset/source 记录是普通字典（catalog.py 注释"内部结构刻意保持为普通字典"），全链路 `dataset["date_col"]`、`params["column_name"]` 字符串索引，键名拼错只在运行时失败；
- "日期 BETWEEN + 代码 IN + 交易日标志"过滤组合在 `_wide`、`asset_codes()`、`_minute`、`_index_component` 等处各自拼接；
- `SourceSpec.to_dict()` 在全仓库无调用方；`SourceSpec.from_key()` 仅 `providers.py` 的内存/仓库 Provider 使用。

### 实现要点

- 为 dataset 记录与 source 落点记录引入 frozen dataclass，`load_config` 的 JSON 解析边界不变，入口处一次性完成类型转换；
- 抽取公共 SQL 谓词构造器（日期区间、代码列表、交易日标志、标识符转义），各 loader 与 `asset_codes()` 复用；
- 删除 `SourceSpec.to_dict()`；保留 `from_key()` 直至确认内存 Provider 的替代构造方式；
- 不改变 `load_group_key` 的构成、诊断事件结构与各 loader 的物理查询行为。

### 验收标准

- Catalog 内部与 loader 中不再出现 dataset/source 记录的字符串索引访问；
- 日期/代码/交易日标志过滤只存在一处构造逻辑；
- `tests/test_smartquant_provider.py` 与 `tests/test_data_provider_backend.py` 全量通过，物理查询次数诊断不变。

## 14. 把 `get_step`/`select_by_pos` 的负索引归一化移出 Compiler

### 目标

消除 `_lower_operator()` 中最后两处按算子名特判的参数归一化，使 Compiler 对普通算子完全通用；这与算子参数校验迁入 `OperatorSpec.validate_params` 是同一类收敛。

### 当前状态

- `compiler.py` `_lower_operator()` 对 `get_step`/`select_by_pos` 硬编码负索引归一化：`params["step"] %= step_count`、`params["pos"] %= length`；
- 该归一化参与语义身份：`get_step(x, -1)` 与 `get_step(x, S-1)` 必须命中同一 CSE Term，因此不能推迟到 Runtime kernel 处理；
- 归一化依赖输入布局（`step_count`/`asset_count`），必须在 layout rule 推导之后执行，现有 `validate_params(params)` 钩子拿不到布局，不能直接复用；
- 其余算子专属逻辑已有明确归属：业务参数校验在 `OperatorSpec.validate_params`，显式坐标变换走文档化的专属 lowering（resample/align_frequency/__select_asset），helper 按名展开属于公式语言定义。

### 实现要点

- 候选方案 A：把 `validate_params` 升级为 `validate_params(params, layouts)`，使其能看到输入布局；缺点是纯参数校验钩子的签名变重；
- 候选方案 B：新增可选的"布局后参数归一化"钩子（如 `normalize_params(params, input_layout)`），与 `validate_params` 职责分开；
- 无论哪个方案，`get_step_layout`/`select_by_pos_layout` 的位置越界校验与参数归一化应保持同一处语义来源，避免两处各自实现取模；
- 顺手评估 `_configuration_literal()` 中硬编码 `neg` 的负数配置字面量是否应上移到公式层。

### 验收标准

- `_lower_operator()` 不再出现按算子名的参数特判；
- `get_step(x, -1)` 与 `get_step(x, S-1)` 产生相同 semantic identity，越界位置仍在编译期报错；
- 全部现有测试通过，行为无变化。
