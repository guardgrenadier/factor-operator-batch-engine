# 项目全景：演进、架构与实现（Agent Handoff）

- 基线：`main@f326fb7` 加工作区生产化修复，2026-09-04
- 目的：让第一次进入仓库的 agent 能区分历史设计与当前实现，并快速定位真实改动边界
- 校验快照：见 §11；生产化修复后重新执行全量测试

## 0. 先读结论

这是一个 Python 因子批计算引擎。调用方用受限 Python 公式文本或不可变 AST 描述一批
因子；Compiler 把多公式合并为一个共享 Term DAG；PhysicalPlanner 按日期和统一历史
回看窗口分区；DataProvider 将逻辑数据引用绑定到 OceanBase 或 parquet；Runtime 以
`float64 T × N × S` 数组拓扑执行；结果可以流式消费、装配到内存，或写入一个验证语义用
的临时因子仓库。

当前代码有两代实现：

- `src/factor_engine/`（排除 `legacy/`）是当前正式批处理管线；
- `src/factor_engine/legacy/` 是旧研究平台、Snapshot Store、Router 和 Reader，只用于追溯
  和旧测试，不应成为新代码依赖。

理解当前系统最重要的四点：

1. **逻辑语义与物理取数分离**：公式只产生 `SourceRefExpr`；物理表、字段、Reader 和
   本次读取范围直到 Provider binding 阶段才出现。
2. **多公式共享一张 DAG**：局部变量名和 formula ID 不参与节点身份，相同表达式会跨
   公式 CSE；未被输出引用的 common input 不会进入计划。
3. **普通算子按位置计算**：2026-08-31 的 ADR 已明确放弃普通算子的业务坐标一致性
   检查。`OperatorTerm` 只按 `ArrayLayout` 的 N/S 做 NumPy 广播；同 shape、不同资产或
   频率身份的值也会逐位置计算。需要变换坐标时必须使用显式算子/helper。
4. **加载规范化只有一个入口**：Reader 只读数据并解释原始坐标；`LoadNormalizer`
   统一负责散布、shape、`float64`、NaN、MASK/CODE 值域和只读授权。Runtime 不重复扫描
   Source 数组，但仍校验 Operator 输出。

仓库没有 CLI、服务层或新研究平台；当前可用入口是 Python API
`BatchFactorEngine.compile/stream/compute`。

## 1. 文档可信度与阅读顺序

仓库文档很多，且混有提案、评审记录、归档和已漂移 TODO。遇到冲突时按以下顺序判断：

1. 当前代码和非 legacy 测试；
2. 已接受的 [ADR-0001](adr/0001-use-positional-array-layout-for-operators.md) 及
   [数组布局与数据加载边界设计](数组布局与数据加载边界设计.md)；
3. 根目录 [FACTOR_ENGINE_DESIGN.md](../FACTOR_ENGINE_DESIGN.md) 的未被 ADR 覆盖部分；
4. [CONTEXT.md](../CONTEXT.md) 的术语定义；
5. TODO、review、walkthrough 和 `legacy/` 文档只作背景材料。

已知文档漂移包括：

- `docs/取数链路演进归档.md` 记录的是 2026-08-18 状态，仍提到已删除的
  `data_provider/datasets.py` 和 schema version 2；当前是 `readers.py +
  query_builders.py + normalize.py`，配置 schema version 3；
- `docs/todo.md` 仍把 `power`、旧 `domain_rules.py`、旧 mapping source 名称等已完成或
  已删除内容写成待办；
- `docs/known_bugs.md` 的 Workspace 内存条目仍引用已移入 legacy 的 `DataRouter`。当前
  Runtime 已主动清理 `loaded/args/value` 局部引用并有弱引用测试，但 NumPy view 的 base
  保留、输出节点生命周期和真实 RSS 上界仍未被完整证明；
- `pyproject.toml` 的项目描述仍写着 “Snapshot-based”，这是 legacy 时代措辞，不代表
  当前正式链路仍依赖 Snapshot Store。

## 2. 项目如何演进到现在

### 2.1 第一代：研究平台与 Snapshot Store

旧入口由 `FeatureManager / FeatureRegistry / Calculator / Executor` 组成。公式先注册为
`FeatureDef`，执行时递归寻找依赖；`FeatureStore` 既保存数据，也固化日期、资产 master
axis 和代码映射；`DataRouter` 按“运行时值 → 已物化 Store → 外部 Reader”的优先级取数。
数组使用带坐标对象的 `FeatureArray`。

这套模型适合个人研究和已注册特征，但把公式定义、物化状态、数据路由和坐标快照耦合在
一起，不适合一次提交大量独立公式、共享子表达式和按任务分块执行。实现现存于：

- `src/factor_engine/legacy/manager.py`
- `src/factor_engine/legacy/registry.py`
- `src/factor_engine/legacy/engine.py`
- `src/factor_engine/legacy/data/`

### 2.2 第二代：新 Batch Engine 契约加兼容 Provider

2026-08-03 左右引入了当前五方法 `DataProvider` 契约：

```text
calendar_dates()
asset_codes()
describe_many()
bind_many()
load_many()
```

当时用 `FeatureStoreDataProvider` 把 Store/Router 适配到新 Compiler/Runtime。它统一了引擎
入口，但坐标和加载仍依赖旧 Snapshot，也没有真正的数据集级批量 I/O。

### 2.3 第三代：任务级独立 Provider

2026-08-11 起落地 `SmartQuantDataProvider`：

- 每个任务从真实日历和行情表解析、冻结资产轴；
- `Catalog` 成为逻辑 source 的唯一描述和绑定来源；
- 相同物理数据集的多个字段合为一个 LoadGroup，一次 SQL 或 parquet scan 读取；
- Provider 直接返回 Runtime 所需的 `float64 T × N × S`；
- 诊断事件随 Provider 生命周期收集。

2026-08-18 移除 `FeatureStoreDataProvider`，旧研究层和旧数据设施整体归入 `legacy/`。
已保存因子不再混入基础数据路由，而由 `RepositoryDataProvider` 组合基础 Provider 和独立
Repository。

### 2.4 当前架构定型：位置布局和加载边界拆分

当前 Git 主线保留的历史较短：

| 日期 | 提交 | 影响 |
| --- | --- | --- |
| 2026-08-30 | `6d170fc` | 以一个大快照导入新旧两套实现、测试和历史文档 |
| 2026-08-31 | `40c9d37` | 先记录数组布局和加载边界设计 |
| 2026-09-01 | `f2cb2f5` | 实现 ADR：`domain_rules.py → layout_rules.py`；删除 `datasets.py`，拆出 Reader、Query Builder 与 LoadNormalizer |
| 2026-09-03 | `f326fb7` | 扩充算子；统一 Memory/Repository 规范化；增强 parquet panel、代码映射冻结、重复坐标/default 校验 |

8 月归档文档列出的 `f2610cc/5580d7a/3dd3053/19ae9b4` 不在当前 Git 对象库中；它们是
历史归档中的演进锚点，不可直接 `git show`。需要还原当前代码变化时，以 `6d170fc` 之后
的可达提交和现有测试为准。

## 3. 当前代码地图

| 路径 | 当前职责 |
| --- | --- |
| `formula.py` | 不可变 Surface AST、受限文本 Parser、FormulaBatch、名称绑定、Python helper |
| `domain.py` | ValueKind、频率/step 坐标、三态 mask、稳定哈希与日期规范化 |
| `model.py` | Domain、Term、Plan、Request、Result 和 DataProvider Protocol |
| `compiler.py` | helper 展开、source 描述、Domain 解析、参数规范化、layout lowering、CSE、lookback |
| `execution.py` | PhysicalPlanner、Runtime、ResultStream、ComputeResult、BatchFactorEngine |
| `operators/` | elementwise、timeseries、cross-section、alignment kernel 与统一注册表 |
| `data_provider/catalog.py` | schema v3 配置、物理数据集/逻辑 source 索引、describe/bind |
| `data_provider/readers.py` | 具名 Reader、RawBatch、InnerCode/SecuCode 翻译、LoadGroup 读取 |
| `data_provider/query_builders.py` | 受控的规范 labels SQL 构造器 |
| `data_provider/normalize.py` | Source 进入 Runtime 前唯一的坐标和值协议边界 |
| `data_provider/backend.py` | ConnectorX OceanBase 与短生命周期 DuckDB/Arrow 后端、SQL 转义、I/O 诊断 |
| `data_provider/smartquant.py` | 正式任务级 Provider 门面、日历/资产轴/代码映射冻结 |
| `providers.py` | `MemoryDataProvider`，用于契约测试和小型内存任务 |
| `repository.py` | 临时因子仓库与组合式 `RepositoryDataProvider` |
| `legacy/` | 旧 Manager/Registry/Calculator/Store/Router/Reader；新实现禁止依赖 |
| `tests/` | 当前管线契约；`tests/legacy/` 单独保护旧实现 |
| `benchmarks/` | 算子生产尺寸和分钟 Arrow 加载的手工 benchmark |

顶层 `factor_engine.__init__` 只导出新管线稳定对象。旧对象必须显式从
`factor_engine.legacy` 导入。

## 4. 一次请求的端到端调用链

```text
公式文本 / Python Expr
        │
        ▼
FormulaBatch ── bind ──> 已解析作用域的 AST
        │
        ▼
Compiler
  helper 展开 → lookback 预分析 → describe sources → resolve output domain
  → lower/intern Terms → CSE → output/layout 校验 → LogicalPlan
        │
        ▼
PhysicalPlanner
  output dates → [read dates(with lookback), write dates, output slice] × partitions
        │
        ▼
Runtime（逐分区）
  bind SourceTerms → LoadGroups → load/normalize → 拓扑执行 → 引用计数释放
        │
        ▼
ResultChunk（partition 外层、formula 内层的稳定顺序）
        ├── ResultStream：调用方流式消费
        ├── compute()：每个 formula 装配一块完整 ndarray
        └── TemporaryFactorRepository.save()：staging 后发布
```

### 4.1 公式层

`FormulaBatch` 包含：

```text
common_inputs: 一个顺序绑定程序，本身不输出
formulas: formula_id -> 各自独立的顺序绑定程序
```

文本 Parser 使用 Python `ast`，但只接受很小的语言子集：简单 `name = expression`、基础
字面量/容器、名称引用、简单函数调用、`+ - * / **`、一元负号和单次比较。不支持属性
访问、控制流、链式比较、任意 Python 执行或 `**kwargs`。

名称规则：

- common input 和公式局部变量都按声明顺序绑定；
- 公式只能引用 common input 和本公式之前的局部绑定；
- 拒绝前向引用、跨公式局部引用、重复定义和覆盖 common input；
- 每个公式最后一个 binding 是输出；最后一个局部名称本身不重要；
- helper/operator 名称是保留字；
- `SourceSpan` 保存 formula ID、行、列，用于 parse/bind/compile 诊断。

Python 的 `source/get_lf/get_hf/get_fund/load_factor/operator` 只构造不可变 Expr，不读取
数据或修改全局状态。当前没有易用的 `FormulaBatch.from_exprs()`；完全无字符串调用需要
直接组装 `Binding/FormulaProgram/FormulaBatch`。

### 4.2 Compiler 的实际顺序

`BatchFactorEngine.compile()` 最终调用 `Compiler.compile()`：

1. `FormulaBatch.bind()` 消除 `SymbolRefExpr`；
2. `_expand_helpers()` 把 `HelperExpr` 改写为 `SourceRefExpr + OperatorExpr`；
3. `_expression_lookback()` 在 source 描述前预分析最大历史窗口；
4. 收集从输出可达的唯一 SourceRef，并调用 `provider.describe_many()` 得到 `InputSpec`；
5. `_resolve_domain()` 查询日历，并用“输出区间 + 预分析 lookback”冻结每种声明资产的轴；
6. 递归 lower 输出表达式，创建 `LiteralTerm / SourceTerm / OperatorTerm`；
7. 通过稳定 semantic hash intern 节点，因此整个 batch 自动 CSE；
8. 校验每个输出 N/S 能广播到目标 OutputDomain；
9. 计算 dependency reference count、最终 `job_lookback` 和 `LogicalPlan.semantic_id`。

这里“compile 不读取数据”应理解为不加载因子值数组。正式 Provider 仍会在初始化和编译
期间读取 Catalog 元数据、交易日历、资产轴，以及被挂载的 SecuCode 数据集所需代码映射。

Semantic identity 的边界：

- Literal：规范化值和 ValueKind；
- Source：逻辑 source identity、`InputSpec`、冻结后的 `TermDomain`；
- Operator：算子名、有名/无名依赖 term ID、规范参数、ValueKind、`ArrayLayout`；
- formula ID、局部变量名、`SourceSpec` 物理表/路径、LoadGroup、chunk size 不进入计划身份。

因此物理表或加载分组变化不应改变等价 LogicalPlan，但 InputSpec、轴或表达式语义变化会
改变它。

### 4.3 PhysicalPlanner 与 Runtime

`PhysicalPlanner` 只做日期执行规划，不修改公式语义。`chunk_size=None` 表示输出日期全域
单分区；否则按固定输出日期数串行切分。每个分区统一使用整个任务的最大 lookback：

```text
read_dates = 当前 write_dates + 日历上最多 job_lookback 个前置 session
write_dates 必须是 read_dates 的连续尾段
```

Runtime 对每个分区：

1. 批量 `bind_many(plan.source_terms, read_domain)`；
2. 按 `load_group_key` 聚合 SourceBinding；
3. 首次遇到某组 SourceTerm 时一次 `load_many(group)`，整组写入 Workspace；
4. 按 `topological_order` 调用算子；
5. 校验 Operator 输出的 dtype、shape、Infinity 和 ValueKind；
6. 按引用计数删除不再使用的非输出 Term；
7. 对每个 formula 取掉 lookback 前缀，并用只读 NumPy view 广播到输出 shape；
8. 产出 `ResultChunk(formula_id, output_slice, values)`。

Runtime 是单进程、分区串行、fail-fast。它不负责重试、并行调度、按公式隔离失败或动态
内存规划。

### 4.4 结果语义

`ResultStream` 是单次消费 iterator。`stream()` 调用时已经完成编译和分区规划，真正的
source load 和 Runtime 计算在消费时发生。只有迭代自然到 `StopIteration` 后
`stream.succeeded` 才为真；此前所有 chunk 都是 provisional。

`compute()` 仅完整消费 stream：每个公式首次出现时分配一个完整 OutputDomain 数组，随后
按 `output_slice` 写入。结果包含：

- `domain`：共同的精确日期、资产、step 坐标；
- `arrays[formula_id]`：`T × N × S float64`；
- `plan`：本次 LogicalPlan；
- `stats`：逻辑加载次数、Workspace entry 峰值、释放记录和 Provider I/O 事件。

`to_dataframe()` 固定生成 `(date, asset, step)` MultiIndex，formula ID 为列。

## 5. Domain、布局和对齐语义

### 5.1 三个坐标对象加一个物理布局

| 对象 | 含义 | 关键字段 |
| --- | --- | --- |
| `DomainSpec` | 调用方声明的任务范围 | start/end、asset_scope、target asset/freq/step count |
| `ResolvedOutputDomain` | 精确结果坐标 | dates、codes、steps、calendar、axis fingerprint |
| `TermDomain` | 仅 SourceTerm 的完整业务坐标身份 | asset/codes、frequency/step count、calendar/fingerprint |
| `ReadDomain` | 某物理分区实际读取范围 | read dates、write dates、source codes/steps、output slice |
| `ArrayLayout` | OperatorTerm 的物理 N/S 和可选溯源提示 | asset_count、step_count、asset_type?、frequency? |

所有非 scalar Runtime 值都是：

```text
T = 当前分区 read_dates 数
N = 当前值的资产维长度
S = 每日 step 维长度
```

`DomainSpec.asset_scope` 必须包含 target asset 和所有 source asset。当前股票 `"all"` 因
数据库缺少独立 universe 表，暂时解析为本任务 horizon 内满足交易标志的有序代码并集；
这是 2026-09-04 已接受、不在本轮修改的生产语义。显式子集保留调用方顺序，并拒绝空、
重复或不在该 horizon 轴内的代码。
正式 Provider 是任务级对象：轴冻结后，用不同日期/selector 再请求同一资产会报错。

### 5.2 ADR-0001 的关键取舍

普通算子只证明物理数组可广播：N 和 S 分别要求相等，或至少一侧为 1。`asset_type` 和
`frequency` 只是无歧义时传播的 hint，用于错误信息和少数专属 lowering，不是兼容性约束。

后果是：

- `stk(N=2)` 和 `cb(N=2)` 可以直接逐位置相加；
- `1d(S=1)` 可以直接广播到日内 `S>1`；
- 不同频率只要 S 相同也可能按位置计算；
- 不可广播时，如果两侧资产 hint 唯一且不同，错误会提示显式映射；
- 业务上的轴顺序正确性由公式作者和数据产品契约负责。

不要根据早期设计文档恢复“普通输入完整坐标一致性检查”，除非先撤销 ADR 并重新评估大量
现有测试。

### 5.3 必须显式表达的变换

- 细日内 → 粗日内/日频：`resample(expr, target_freq, method=mean|sum|std|last)`；
- 粗日内 → 细日内：`align_frequency(expr, target_freq, method="ffill")`；
- 单资产/指数选择：`select_asset` 或 `select_index_feature` helper，在编译期把代码变成位置；
- 股票值投影到转债轴：`project_stk_to_cb(values)`，展开为通用 `lookup_by_col` 加任务级
  `cb.1d.underlying_stk` CODE source；
- step/资产归约：由明确带 layout rule 的 step、cross-section、member operator 完成。

频率表固定支持 `1d/1min/5min/15min/30min/60min`。标准 1 分钟轴当前是 237 个 step，
其他分钟频率按 240 分钟规则推导。非标准 `target_step_count` 会得到位置标签，但
`resample/align_frequency` 要求完整标准源 step 轴。

### 5.4 Lookback

每个 OperatorSpec 声明本地日期 lookback：

```text
term.lookback = local_operator_lookback + max(input.lookback)
job.lookback  = max(output.lookback)
```

所有 Source 在一个分区内都读取 job-wide 最大窗口，尚无 per-Term/per-source offset。
日期轴 rolling、delay、有限 ffill 会增加 lookback；step 轴 delay 不增加日期历史。负
periods 会在编译期拒绝，因为系统没有 future horizon，不能读取未来数据。

## 6. 数据输入架构

### 6.1 五层对象不要混用

```text
SourceRefExpr
  公式需要什么；逻辑 key + 影响语义的参数
        │ describe_many
        ▼
InputSpec
  编译需要的 asset/frequency/step_count/ValueKind/calendar
        │ lower
        ▼
SourceTerm
  DAG 中的逻辑输入身份；仍无物理表和当前分区
        │ bind_many(read_domain)
        ▼
SourceBinding
  term_id + SourceSpec + 精确 ReadDomain + LoadGroupKey
        │ load_many
        ▼
只读 float64 T × N × S ndarray
```

`SourceSpec` 才包含 source/table/field/reader/query_builder/物理 params。不要把这些字段放入
公式 AST、Compiler 特判或 LogicalPlan semantic identity。

### 6.2 SmartQuantDataProvider 的任务生命周期

Provider 初始化时加载 schema v3 `data_sources.json`，先由 `validate_config()` 在无 I/O
阶段校验逻辑 key、数据集身份、Reader/Query Builder、频率、ValueKind 和代码身份，再建立
普通物理数据集和逻辑 source 索引。随后可能扫描 SQL/parquet 字段、动态查询基本面 Item。
`include_tables` 是白名单，可限制挂载和初始化元数据读取。

当前内置配置有 8 个 `source_tables`：

- 股票、转债、指数日频 SQL 面板；
- 股票/转债 1 分钟 parquet、股票 5 分钟 parquet；
- `OpsData` 和 `OpsIntra` 两类日频 parquet panel。

另有 6 个显式特殊 source：复权因子、不可交易 mask、转债正股关系、申万行业代码、
CSI300 权重和成员 mask。基本面 source 根据数据库元数据动态登记，不是静态清单。

Compiler 解析 Domain 时，Provider：

- 从 `SmartQuant.JY_TradingDayNew` 缓存交易日历；
- 从各资产权威行情表按完整 horizon 冻结 `stk/cb/idx` 的有序 InnerCode 轴；
- 仅当已挂载数据集使用 `secu_code` 时冻结对应 InnerCode↔SecuCode 映射；
- 股票使用静态映射表，转债使用带日期的资产轴映射；
- 将查询和 cache hit 写入 `diagnostics`。

因此一个正式 Provider 实例应对应一个任务，不应作为多任务、不同 Domain 的长生命周期
全局单例。

### 6.3 Catalog、Reader、Query Builder、Normalizer

职责链如下：

```text
data_sources.json
  → Catalog：描述 Dataset/Source，选择具名 reader/query_builder
  → Reader：执行 I/O、解释物理坐标、产出 RawBatch
  → LoadNormalizer：散布和授权 Runtime 数组协议
```

Reader 清单：

| Reader | RawBatch mode | 用途 |
| --- | --- | --- |
| `sql_reader` | `labels` | 执行具名 SQL builder，输出 date/asset/可选 step/value 列 |
| `fundamental` | `labels` | PIT/rank 查询，并把报告期 rank 解码为 step |
| `parquet_bars` | `flat` | 带日内 step 的日期分区 parquet，Arrow 流式返回 canonical flat position |
| `parquet_panel` | `flat` | S=1 的日期分区 parquet 面板 |
| `cb_stock_map` | `static` | 无日期转债→正股关系，沿 T 广播并可转为任务股票轴位置 |

SQL Query Builder 清单：

- `panel_fields`：宽表多字段/常量投影、交易标志和 selector；
- `adjust_factor`：as-of 复权因子；
- `untradable`：多个交易状态列派生 mask。

新增 SQL 差异但结果仍是规范 labels 时，应加 Query Builder；只有出现新的物理结果布局或
坐标解码语义才加 Reader。JSON 不允许注入任意 SQL 模板或自定义 Python 插件。

`RawBatch` 的四种 mode：

- `labels`：DataDate + InnerCode + 可选 Step + values；
- `flat`：已映射到 ReadDomain 的 flat integer positions + values；
- `static`：无日期、按资产关系；
- `dense`：已对齐完整数组，由 Memory/Repository Provider 使用。

`LoadNormalizer` 对它们统一执行：

- 在 canonical position 上拒绝批内和跨批重复、越界、非整数坐标；
- 预分配并散布到完整 `T × N × S`；
- 数值转换为 `float64`，NULL/缺行/Infinity 归一为 NaN；
- 校验 MASK 只能是 `0/1/NaN`，CODE 只能是整数/NaN；
- 校验并应用显式 default；
- static 值沿日期轴广播；
- 输出 term_id 集合完整的只读数组。

Parquet Reader 使用 DuckDB + Arrow batch，减少把全部原始行先物化为 pandas DataFrame 的
中间内存，但 Normalizer 最终仍会为每个 Source 分配完整分区数组。

### 6.4 LoadGroup

`SmartQuantDataProvider.bind_many()` 的 LoadGroup key 包含 Reader、Query Builder、物理表、
影响行集合/坐标解码的共享参数和 ReadDomain；字段、constant、default、ValueKind、projection
等 field-level 参数不拆组。因此同表 close/volume 可在一次 SQL 或 scan 中读取。

Runtime 的 `stats.load_calls` 是逻辑 `load_many()` 次数；物理查询的字段、范围、行数、
字节、耗时和状态应查看 `stats.provider_events`。两者不要混为同一个性能指标。

## 7. Operator 体系

当前默认 Registry 有 134 个注册名（含别名）：42 个 elementwise、48 个 timeseries、
42 个 cross-section、2 个 alignment。kernel 主要用 NumPy；循环密集路径有 62 个
`@njit` kernel。Registry 构造时会拒绝重复名称、名称不一致以及与函数签名不一致的
变长/可选动态输入。

`OperatorSpec` 是 Compiler 与 Runtime 的共同契约：

```text
name
func
input_kinds / VariadicInput
output_kind
date_lookback
layout_rule
optional_inputs
validate_params
```

Compiler 用 Python 函数签名绑定调用：前置参数和 `optional_inputs` 是动态 Term 输入，其余
必须是编译期字面量配置。非空函数默认值会先补齐，再进行规范化、校验、lookback 和
semantic identity；省略默认值与显式写默认值会命中同一个 CSE Term。`sample_mask`、
`weight`、`where(..., y)` 等可作为动态 keyword Term 或 Literal 输入。

ValueKind 只有三种，物理 dtype 始终是 `float64`：

- `NUMERIC`：普通数值，非有限结果归一为 NaN；
- `MASK`：三值逻辑 `0=False, 1=True, NaN=Missing`；
- `CODE`：有限整数或 NaN。

普通算子默认使用 `broadcast_layout`。改变 shape 的算子必须选择明确的 layout rule，例如
`asset_reduce_layout`、`step_reduce_layout`、`get_step_layout`、`slice_step_layout`、
`lookup_by_col_layout`；需要 source 频率/代码参与的 `resample/align_frequency/asset select`
在 Compiler 中走专属 lowering。

Runtime 的 `_validate_operator_result()` 会把结果转为 `float64`，校验 MASK/CODE、精确原生
shape，并把数值 Infinity 改为 NaN。不要因为 Source 已被 LoadNormalizer 校验就删除这个
Operator 边界。

## 8. 临时持久化与旧实现

### 8.1 TemporaryFactorRepository

临时仓库用于验证 `ResultStream → 保存 → load_factor()` 闭环，**生产不可用**：

- 流式 chunk 写入唯一 staging 目录；
- 自然消费完成后写 metadata，再用目录 rename 发布每个 formula；
- 失败时清理 staging 和本轮已发布目录；
- 保存 dates/codes/steps 和 chunk slice；
- 加载时只读与 ReadDomain 重叠的 `.npy` chunk；
- `RepositoryDataProvider` 把 `factor:<id>` source 与任意基础 Provider 组合。

项目当前没有落盘需求；该实现不支持覆盖、增量 upsert、并发、版本、崩溃一致性或正式
数据格式演进。不得接入生产，也不要基于当前目录布局构建长期协议。

### 8.2 Legacy 边界

以下概念只属于旧管线：`FeatureManager`、`FeatureRegistry`、`Calculator`、
`FeatureStore`、`DataRouter`、`SmartQuantSourceReader`、`FeatureArray`、Snapshot manifest。

新功能默认不修改 `legacy/`，也不从当前 core import legacy。只有修复明确的旧接口回归时
才同时改 `tests/legacy/`。legacy notebook 依赖已删除的兼容层，不能视为当前可运行示例。

## 9. 当前设计与实现特点

### 9.1 做得比较明确的边界

- 公式 AST、编译 IR、物理绑定和执行数组是四层不同对象；
- `ComputeRequest` 只描述语义，`ExecutionOptions` 目前只描述 chunk size；
- CSE、lookback 和参数校验在读取数据前完成；
- 物理 I/O 路由不进入 LogicalPlan；
- Source 和 Operator 各有独立、唯一的值协议校验边界；
- 轴选择和映射尽量在编译期把业务 code 降成运行时 position；
- streaming 的成功条件和临时仓库 staging 避免把半条流误当作已提交结果；
- 测试大量使用 MemoryProvider/fake backend，能在无真实数据库时覆盖核心契约。

### 9.2 刻意接受的简化

- 单进程、串行分区、全任务 fail-fast；
- 所有数组 `float64 + NaN`，不维护 nullable dtype 或紧凑 mask/code；
- 所有 Source 使用 job-wide 最大 lookback，不做精细 offset；
- 普通算子只看位置和 shape，不保护业务坐标一致性；
- Provider 每任务创建，不建设跨任务 cache；
- Reader/Query Builder 是代码内固定注册表，不建设插件框架；
- 只提供显式固定日期 chunk，不按内存预算自适应；
- 当前股票 `"all"` 使用任务 horizon 内交易代码并集，待数据库提供权威 universe 表后再评估；
- 默认挂载全部数据集的开销、空物理结果按 Missing 处理、当前分钟 Arrow batch 与 chunk
  默认值均已在 2026-09-04 生产评审中接受，不在本轮修改；
- 当前没有生产落盘需求，TemporaryFactorRepository 明确禁止生产使用；
- 研究 Registry、分布式执行均延后。

### 9.3 仍需谨慎的现实限制

- `stream()` 可限制分区工作集，但返回 chunk 常是 Workspace 数组的 view；消费者保留 chunk
  会延长底层 buffer 生命周期。`compute()` 必然另外持有每公式完整结果数组；
- `peak_workspace_values` 只统计字典 entry，不统计真实 nbytes、共享 base、Provider 内存或
  结果装配，不能当作 RSS 峰值；
- 真实 OceanBase/CephFS 行为依赖部署数据。仓库测试主要使用 fake backend/临时 parquet，
  全绿不等于生产数据字典和性能已经验证；
- `Catalog` 仍以普通 dict 贯穿配置，字段名错误多在运行时发现；
- Formula Python Builder 不够友好，字符串仍是主要入口；
- 只支持单一兼容日历和预定义频率；没有 future read horizon；
- 同 shape 错轴是 ADR 接受的风险，不会由 Runtime 兜底；
- 仓库未配置可见 CI、pre-commit、容器或发布流程，`0.1.0` 也不代表稳定兼容承诺。

## 10. Agent 修改项目时的最短路径

### 10.1 新增/修改公式语法或 helper

通常检查：

```text
formula.py                 AST、Parser、DEFAULT_HELPERS、Python helper
compiler.py                helper 展开或专属 lowering
tests/test_formula.py      解析/作用域/AST 等价性
tests/test_batch_engine.py 完整编译执行行为
```

helper 只能展开为已有 SourceRef/Operator；不要在 helper 或 Compiler 中直接读取业务数据。

### 10.2 新增普通算子

最小闭环：

1. 在 `operators/elementwise.py`、`timeseries.py` 或 `cross_section.py` 写纯 kernel；
2. 在 `operators/registry.py` 登记 ValueKind、输入、参数、lookback、layout rule；
3. 只有 shape 改变时才在 `operators/layout_rules.py` 增加或复用规则；
4. 添加数值、NaN/Mask、编译参数、shape 和 whole/chunk 等价测试。

不要为普通算子在 Compiler 增加名字特判；专属 lowering 只留给确实依赖 source
频率/代码的显式坐标变换。

### 10.3 新增数据源

先选最小路径：

- 已有挂载宽表的新列：让 Catalog 扫描即可；
- 特殊单字段或语义参数：只加 `data_sources.json.sources`；
- 新表但已有物理布局：加 `source_tables` 并复用 Reader/Query Builder；
- 新 SQL 语义、同 labels 输出：加具名 Query Builder；
- 新物理布局/坐标解码：才加 Reader；
- 新资产类型：还需登记 `_ASSET_AXES/_CODE_MAPS` 并补 Domain/轴测试。

Source 值最终必须经过 `normalize_batches()`；不要在 Runtime 增加 Provider 特判。
配置改动必须先通过 `validate_config()`；公式 semantic params 不得覆盖数据集物理坐标参数。

### 10.4 修改执行或结果

重点同时检查：

- `LogicalPlan.reference_counts` 与跨输出 CSE；
- whole-domain 和多个 `chunk_size` 的数值/NaN 等价；
- `read_dates` 的 lookback 前缀和 write suffix 假设；
- ResultStream 单次消费、自然完成、异常清理；
- NumPy view/base 生命周期与 `compute()` 完整结果分配；
- Provider diagnostics 是否仍能区分逻辑 load 与物理 I/O。

### 10.5 不要误改的地方

- 不因 review/TODO 中的旧描述恢复 `datasets.py`、`domain_rules.py` 或
  `FeatureStoreDataProvider`；
- 不把 `legacy` 类型重新导出到顶层；
- 不用 SourceSpec 物理路径参与 LogicalPlan identity；
- 不在 Reader 重复做最终 dtype/ValueKind/shape 规范化；
- 不把业务坐标一致性检查悄悄塞回普通 layout rule；
- 不把临时 Repository 文件格式当成正式协议。

## 11. 验证与本地运行

环境要求 Python `>=3.11,<3.14`，依赖由 `uv.lock` 固定。常用命令：

```bash
uv run --frozen pytest -q
uv run --frozen ruff check src tests
```

真实 SmartQuant 任务需要 `.env` 或环境变量中的 `OB_USER/OB_PASSWORD`，可选覆盖
`OB_HOST/OB_PORT`；`.env` 不提交。纯核心测试不需要真实数据库。

当前测试快照：

- 共收集 279 个 pytest case；
- 其中当前管线有 177 个 test function，legacy 有 15 个 test function，参数化展开后得到
  279 case；
- 测试覆盖公式作用域、CSE/lookback、分块等价、layout/频率/资产变换、三值 mask、134 个
  operator 的主要数值契约、LoadNormalizer、SQL builder、SmartQuant fake backend、临时仓库；
- benchmark 需要手工运行，不属于 pytest 门禁。

生产尺寸算子基准入口：

```bash
uv run --frozen python benchmarks/operator_benchmark.py --preset daily --days 252
uv run --frozen python benchmarks/operator_benchmark.py --preset 5min --days 20
uv run --frozen python benchmarks/profile_minute_arrow.py --rows 5000000
```

## 12. 推荐的进一步阅读

- 公共入口与最小示例：[README.md](../README.md)
- 统一术语：[CONTEXT.md](../CONTEXT.md)
- 总体设计（注意 ADR 覆盖项）：[FACTOR_ENGINE_DESIGN.md](../FACTOR_ENGINE_DESIGN.md)
- 当前权威边界：[数组布局与数据加载边界设计.md](数组布局与数据加载边界设计.md)
- 历史详细调用链（部分正文已过期）：[调用链_最新.md](调用链_最新.md)
- 扩展指南：[新增算子与数据源指南.md](新增算子与数据源指南.md)
- 取数代际历史：[取数链路演进归档.md](取数链路演进归档.md)
- 资产映射细节：[资产轴对齐规则.md](资产轴对齐规则.md)
- 频率/step 细节：[频率与第三维对齐规则.md](频率与第三维对齐规则.md)
