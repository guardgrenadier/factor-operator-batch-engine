# 调用链设计解读：从公式字符串到因子数组

> 注意：2026-08-31 起算子布局与数据加载边界以
> [`数组布局与数据加载边界设计.md`](数组布局与数据加载边界设计.md) 及
> [ADR-0001](adr/0001-use-positional-array-layout-for-operators.md) 为准：
> 本文中 `OperatorSpec.domain_rule`（默认 `numpy_domain`）已由
> `layout_rule`（默认 `broadcast_layout`）取代，普通算子不再比较业务坐标身份。

- 更新日期：2026-08-19
- 文档类型：Walkthrough / Explanation（走读式解读）。按真实调用顺序逐步讲解
  每一步的**设计目的**和**数据形态变化**，并全程带一个可运行例子。
- 与其他文档的关系：
  - [`FACTOR_ENGINE_DESIGN.md`](../FACTOR_ENGINE_DESIGN.md)：设计基线，回答“系统长什么样”；
  - [`调用链_最新.md`](调用链_最新.md)：参考型调用链，回答“代码实际怎么跑”；
  - 本文：解读型走读，回答“**每一步为什么这样设计、数据在里面变成了什么**”。
- 重新熟悉项目时建议顺序：本文 -> `调用链_最新.md` -> 按需抽查源码。
- 本文例子使用正式 `SmartQuantDataProvider` 直连 OceanBase 真实数据库
  （需在仓库根 `.env` 配置 `OB_USER/OB_PASSWORD/OB_HOST/OB_PORT`），
  文中所有中间产物均为真实运行输出。

## 0. 总览：一条公式要经过哪些形态

一次计算中，同一份“因子定义”会依次变成五种形态，每一步都把它变得更规范、
更接近可执行：

```text
字符串公式
  --from_text-->  Surface AST（带 SymbolRefExpr 占位）
  --bind-->       展开后的 AST（无符号引用，只剩 Helper/Operator/Literal/SourceRef）
  --expand_helpers-->  规范 AST（只剩 OperatorExpr / SourceRefExpr / LiteralExpr）
  --lower-->      Term DAG（带 TermDomain、lookback、语义身份）
  --partitions + runtime-->  ResultChunk -> 完整 T × N × S 数组
```

设计主线只有三条，理解了它们，每一步的存在理由就都清楚了：

1. **分离“算什么”与“在哪个 Domain 上算”与“怎样执行”**——公式、DomainSpec、
   ExecutionOptions 分别承担，互不渗透；
2. **编译期做全部语义决策，运行期只执行计划**——坐标系、广播合法性、
   回看长度都在 compile 阶段冻结；
3. **物理细节永远不进入逻辑身份**——表名、reader、load group 只出现在
   分区绑定期，因此同一逻辑任务可以切换物理实现而计划身份不变。

## 1. 贯穿全文的例子

```python
from factor_engine import (
    BatchFactorEngine, ComputeRequest, DomainSpec,
    ExecutionOptions, FormulaBatch, SmartQuantDataProvider,
)

provider = SmartQuantDataProvider()  # 从 .env 读取 OB 配置，任务级、一次性冻结

batch = FormulaBatch.from_text(
    common_inputs="""
        close = source("stk.1d.ClosePrice")
        volume = source("stk.1d.TurnoverVolume")
    """,
    formulas={
        "alpha_1": """
            mean = ts_mean(close, 2)
            factor = mean / volume
        """,
        "alpha_2": "factor = (close + volume) * 2",
        "alpha_3": "part = close + volume\nfactor = part - 0.5",
    },
)

request = ComputeRequest(
    domain=DomainSpec(
        start="20241202",
        end="20241206",
        asset_scope={"stk": [3, 6]},
        target_asset="stk",
        target_freq="1d",
        target_step_count=1,
    ),
    batch=batch,
)
```

`SmartQuantDataProvider()` 构造时会解析 `data_provider/data_sources.json` 并扫描
数据库 catalog（information_schema、基本面 Item 清单）冻结一份任务级目录；
日历与资产轴在首次用到时各查询一次并缓存。`stk.1d.ClosePrice` 与
`stk.1d.TurnoverVolume` 都落在物理表 `SmartQuant.ReturnDaily` 上。

`asset_scope={"stk": [3, 6]}` 是显式 InnerCode 子集（这两只股票在所选周内
每个交易日都有行情）。如果写 `{"stk": "all"}`，同一周会冻结约 5100 只
股票的完整 master axis；文档为了展示方便只用两只。

三个公式刻意覆盖不同要点：`alpha_1` 有时序窗口（产生 lookback），
`alpha_2` 与 `alpha_3` 用不同局部写法表达同一个 `close + volume`
（展示跨公式 CSE）。

## 2. 四个请求对象：先切分职责

```text
FormulaBatch      算什么：一批公式 + 名称作用域
DomainSpec        在哪个 Domain 上算：日期区间、资产范围、目标频率/step
ComputeRequest    把上面两者组合成一次任务
ExecutionOptions  怎样执行：当前只有 chunk_size
```

设计目的：公式作者只写 FormulaBatch，任务发布者才选 Domain，性能调优只动
ExecutionOptions。三者任何一维变化都不需要重新表达另外两维。例如同一
FormulaBatch 可以在股票 1d 域和可转债 1d 域上分别计算，LogicalPlan 语义
身份中也不包含日期区间（见 §8）。

## 3. from_text：字符串 -> Surface AST

代码：`formula.py` 的 `FormulaParser`。

### 3.1 做什么

`FormulaBatch.from_text()` 用 Python 标准库 `ast` 做语法解析，但只接受引擎
支持的语法子集：

- 每条语句必须是简单赋值 `name = expression`；
- 表达式只允许字面量、名称引用、函数调用、二元数学/比较运算符；
- Python 运算符会被翻译成 operator 调用：`a + b` -> `OperatorExpr("add", ...)`，
  `a >= b` -> `OperatorExpr("greater_equal", ...)`；
- 不允许 if/for/下标/属性访问/lambda 等任意 Python 语义。

parse 之后，`alpha_1` 的程序是：

```text
mean   = Op:ts_mean(«close», 2)
factor = Op:divide(«mean», «volume»)
```

其中 `«close»` 表示 `SymbolRefExpr("close")`——一个**尚未绑定**的名称占位符。

### 3.2 为什么这样设计

- **用 Python ast 只是复用语法解析器**，不是要支持 Python。引擎需要的语法
  是 Python 的真子集，这样公式可读、可复制，同时引擎完全掌控语义。
- **parse 阶段不解析名称、不执行任何 NumPy**。所有名称一律先记成
  `SymbolRefExpr`，原因有二：
  1. 作用域规则（common_inputs 可见性、禁止前向引用等）属于**绑定层**职责，
     与语法解析正交，先 parse 后 bind 让两层各自可测；
  2. parse 得到的 AST 是不可变的纯数据，可以被安全地检查、打印和转换，
     不会出现“边解析边算出数组”的副作用。
- **字符串里的 `close + volume` 永远先变成 `OperatorExpr("add", ...)`**。
  这是整条链路“一切计算都走算子”的起点——后续每一层只需要处理统一的
  节点类型，不需要再区分“用户写的加法”和“引擎生成的加法”。

## 4. bind：把符号引用递归展开成完整表达式

代码：`formula.py` 的 `FormulaBatch.bind()` / `_bind_program()` / `_resolve_symbols()`。

### 4.1 做什么

bind 按作用域规则把每个 `SymbolRefExpr` **替换成它所指向的完整表达式**，
并返回每个公式的最终输出表达式。本例中：

```text
alpha_1: divide(
           ts_mean(Helper:source("stk.1d.ClosePrice"), 2),
           Helper:source("stk.1d.TurnoverVolume"))

alpha_2: multiply(
           add(Helper:source("stk.1d.ClosePrice"), Helper:source("stk.1d.TurnoverVolume")),
           2)

alpha_3: subtract(
           add(Helper:source("stk.1d.ClosePrice"), Helper:source("stk.1d.TurnoverVolume")),
           0.5)
```

注意两点变化：

- 所有 `«name»` 都消失了，取而代之的是被引用表达式的**完整内联拷贝**；
- `close`/`volume` 现在显示为 `Helper:source(...)`——它们仍是 HelperExpr，
  还没有变成 SourceRefExpr。

### 4.2 为什么这样设计

- **绑定即展开（substitution），不是建立指针**。展开后每个公式输出就是一棵
  自包含的表达式树，不再依赖任何名称环境；后续阶段不需要再携带作用域表。
- **作用域规则在绑定层一次性强制执行**：common_inputs 先绑定并作为每个
  公式的初始环境；公式内只能引用本公式前面定义的名称；禁止前向引用、
  跨公式引用局部名、局部名覆盖 common input、使用 operator/helper 名作为
  binding 名。这些错误都在 bind 阶段以 `SymbolBindingError` 报出，带公式名。
- **binding 名不进入表达式的语义内容**。`alpha_2` 的 `(close + volume)` 是
  内联写的，`alpha_3` 的同一子表达式叫 `part`，但展开后两棵树结构完全
  相同——这是 §7 跨公式 CSE 能够成立的前提。局部名只服务于人类可读性。
- bind 需要知道哪些名字是保留字（operator 名 + helper 名），因此由
  Compiler 传入 registry 名称集合调用。

## 5. expand_helpers：把便利写法降成规范 AST

代码：`compiler.py` 的 `_expand_helpers()`。

### 5.1 做什么

注册的 helper 只有 9 个：`source` / `get_lf` / `get_hf` / `get_fund` /
`load_factor` / `select_asset` / `select_index_feature` / `index_member_stat` /
`project_stk_to_cb`。这一步把每个 `HelperExpr` 改写成规范节点：

```text
alpha_1: divide(ts_mean(SourceRef("stk.1d.ClosePrice"), 2),
                SourceRef("stk.1d.TurnoverVolume"))
alpha_2: multiply(add(SourceRef("stk.1d.ClosePrice"), SourceRef("stk.1d.TurnoverVolume")), 2)
alpha_3: subtract(add(SourceRef("stk.1d.ClosePrice"), SourceRef("stk.1d.TurnoverVolume")), 0.5)
```

展开后，表达式树中只可能出现三种节点：`OperatorExpr` / `SourceRefExpr` /
`LiteralExpr`。

### 5.2 为什么这样设计

- **helper 是公式层的便利写法，不是计算节点**。`get_lf("stk", "ClosePrice")`
  只是 `source("stk.1d.ClosePrice")` 的简写；`index_member_stat(..., method="mean")`
  只是 `member_mean(...)` 的语义包装。让它们在进入计划前消失，LogicalPlan
  的节点类型就收敛到三种，后面每一层（lowering、执行、校验）都只需要处理
  固定的三种 Term。
- **展开是纯结构改写，不验证语义**。此时引擎还不知道
  `stk.1d.ClosePrice` 是不是合法 source——合法性要等 `describe_many()` 查询
  Provider 才能判定。职责分离：语法糖在编译早期拆掉，数据语义交给 Provider。
- **`resample` 和 `align_frequency` 不是 helper**。它们在 parse 阶段就直接
  产出 `OperatorExpr`，`_expand_helpers()` 只是递归透传。因为频率转换是
  真正的语义算子（有 kernel、有 domain rule、有 lookback），而不是写法糖；
  把它留在 operator 体系里才能获得统一的校验和执行路径。

## 6. describe_many：编译期的“只问不取”

代码：`Compiler.compile()` -> `provider.describe_many(refs)`。

### 6.1 做什么

Compiler 收集展开后所有公式里的 `SourceRefExpr`，按引用身份去重，然后只向
Provider 问一句话：“这些逻辑输入是什么 Domain 和类型？”得到的答案是
`InputSpec`，不发生任何数据读取：

```text
stk.1d.ClosePrice      -> asset_type=stk freq=1d steps=1 kind=numeric calendar=cn_a_share
stk.1d.TurnoverVolume  -> asset_type=stk freq=1d steps=1 kind=numeric calendar=cn_a_share
```

对 SmartQuantDataProvider，这一步由任务级 Catalog 完成：逻辑 key 在
`data_sources.json` 显式配置与扫描出的物理字段（如 ReturnDaily 的全部列）
中查表解析为数据集与字段，再汇总成 `InputSpec`。目录在 Provider 构造时
就已冻结（catalog_snapshot），describe 本身不再产生数据级 I/O。

### 6.2 为什么这样设计

- **编译必须知道每个 source 的原生坐标**（频率、step 数、资产类型、日历），
  否则无法推导中间 Term 的 TermDomain，也无法决定哪些输入混算需要显式对齐。
- **但编译不应该触碰物理数据**。`InputSpec` 只含语义信息，不含表名、字段、
  reader 配置。物理信息由 §10 的 `bind_many()` 在分区粒度产生
  （`SourceSpec`），因此：
  - `LogicalPlan.semantic_id` 不含任何物理细节，同一任务换等价物理路径
    （例如同一宽表改走 DuckDB）计划身份不变；
  - `engine.compile()` 可以零 I/O 完成，适合作为 review 编译结果、检查
    CSE 和 lookback 的入口。
- **去重发生在编译期**：三个公式共引用 2 个逻辑 source，最终只会有 2 个
  SourceTerm，物理上最多按 load group 合并读取。

## 7. domain 解析：把“模糊的范围”冻结成精确坐标

代码：`compiler.py` 的 `_resolve_domain()`。

### 7.1 做什么

输入是用户声明的 `DomainSpec` 加所有 `InputSpec`，输出是冻结的
`ResolvedOutputDomain`：

```text
domain.dates    = ["20241202", "20241203", "20241204", "20241205", "20241206"]
domain.codes    = [3, 6]
domain.steps    = [0]
domain.calendar = "cn_a_share"
domain.shape    = (5, 2, 1)
```

步骤依次是：校验 target_asset 在 asset_scope 中；从全部 InputSpec 确定唯一
calendar；取 provider 日历并截取 start~end；**把输出日期窗口向前扩展
pre_lookback 个日期**得到 `axis_dates`；校验所有输入资产类型都已声明在
asset_scope；逐资产调用 `provider.asset_codes(asset, axis_dates, selector)`
冻结有序代码轴和 axis_fingerprint；最后按 target_freq + target_step_count
生成 step 轴。

### 7.2 为什么这样设计

- **“范围”不是“坐标”**。`start/end` + `"all"` 是调用方的意图，不同 provider
  的日历和资产清单不同，必须在编译期就固化成唯一确定的 dates/codes/steps，
  之后所有 Term shape、chunk 切片、结果装配都以它为唯一事实。
- **asset_scope 描述的是资产类型集合，不是输出轴的成员过滤**。
  `{"stk": "all"}` 表示任务冻结一条完整有序的 stk master axis（SmartQuant
  实现下就是在扩展日期窗口内 `IfTradingDay=1` 的 DISTINCT InnerCode，本例
  一周约 5100 只），公式里的每个 stk source 都在这条轴上取值；显式子集
  （本例的 `[3, 6]`）保留调用方顺序，并校验这些代码确实出现在窗口行情中，
  重复或未知代码直接失败。
- **日期窗口要带 lookback**：后续 source 绑定要用这个窗口生成自己的
  ReadDomain，窗口不够长就无法满足 `ts_mean` 这类算子的历史需求。
- **同一 batch 只能有一个 calendar**。混用日历意味着两个日期序列，无法
  形成统一的 T 轴，属于设计上的硬禁止。

`frequency` 和 `step_count` 在这里是独立字段：日频行情是 `1d + 1 step`，
日频基本面可以是 `1d + Q steps`。这解释了为什么 DomainSpec 同时需要
`target_freq` 和 `target_step_count`。

## 8. lowering：AST -> 带身份的 Term DAG（含 CSE 与 lookback）

代码：`compiler.py` 的 `_lower()` / `_intern()`。

### 8.1 做什么

每个公式的输出表达式被自顶向下降为 Term，三种 Expr 一对一映射三种 Term：

```text
LiteralExpr  -> LiteralTerm（domain=None）
SourceRefExpr -> SourceTerm（保留 InputSpec 的原生 domain）
OperatorExpr  -> OperatorTerm（由 domain_rule 推导输出 domain）
```

每个 Term 的身份由**语义 hash** 决定：operator 名 + 具名输入 term +
参数字典 + 输出 ValueKind + 输出 TermDomain（source/literal 同理）。
`_intern()` 保证同一语义全批次只存在一个 Term，重复引用直接复用。

本例编译出的 DAG（term_id 截短显示）：

```text
拓扑顺序中的 Term：
term_66d4  Source   "stk.1d.ClosePrice"      stk N=2 1d S=1  lookback=0  refs=2
term_d1c2  ts_mean  window=2                 stk N=2 1d S=1  lookback=1  refs=1
term_94d3  Source   "stk.1d.TurnoverVolume"  stk N=2 1d S=1  lookback=0  refs=2
term_f445  divide                            stk N=2 1d S=1  lookback=1  refs=0  <- alpha_1
term_9a9d  add                               stk N=2 1d S=1  lookback=0  refs=2
term_a84d  Literal  2                        domain=None     lookback=0  refs=1
term_3c13  multiply                          stk N=2 1d S=1  lookback=0  refs=0  <- alpha_2
term_a673  Literal  0.5                      domain=None     lookback=0  refs=1
term_bba6  subtract                          stk N=2 1d S=1  lookback=0  refs=0  <- alpha_3

job_lookback = 1
```

读这张表可以验证前面所有设计承诺：

- 3 个公式加 common_inputs 共 7 条 binding，最终只有 9 个 Term。`close`、
  `volume` 各自只有**一个** SourceTerm（refs=2，被两条使用路径共享）；
- `alpha_2` 内联的 `(close + volume)` 和 `alpha_3` 里名为 `part` 的同一
  表达式合并成**同一个** `term_9a9d`（refs=2）——局部命名不同也能 CSE；
- `ts_mean` 的 `window=2` 带来 `lookback=1`，并沿 DAG 传给输出
  `divide`；`job_lookback` 取全部输出的最大值；
- 每个 Term 的 TermDomain 都写着它**自己的**坐标身份
  （asset_type + N + 频率 + S），不是任务 shape。

### 8.2 为什么这样设计

- **CSE 是语义 hash 的自然结果，不是额外优化 pass**。Term 身份只由结构语义
  决定，相同的计算“天然”落在同一个 hash 上；不同局部变量名、不同公式、
  不同声明顺序都不影响合并。
- **TermDomain 逐节点推导，而不是统一贴任务 Domain**。普通算子由
  `OperatorSpec.domain_rule`（默认 `numpy_domain`）合并输入坐标——
  step 轴、频率、资产轴各自独立合并，且只有明确的 singleton 广播被允许；
  shape-changing 算子（reduce/select/lookup 等）使用专用规则。这样
  “两个 shape 相同但坐标身份不同的轴混算”会在编译期直接报 DomainError，
  而不是运行期得到错位数组。
- **lookback 沿 DAG 累计**：`term.lookback = operator 自身 date_lookback +
  输入中的最大累计 lookback`。链式 `ts_mean(ts_mean(close, 5), 5)` 会累计成
  正确的总回看，物理层拿到的只需是一个任务级最大值。
- **编译末尾还做两件事**：
  1. `_validate_output_domain()`：每个输出 Term 的原生 Domain 必须能按
     已确认规则广播到任务 Domain（calendar 完全一致；资产轴 N=1 可广播、
     N>1 必须与目标轴同一；频率同频或 1d+step1 广播；step 数只能是 1 或
     target）。这里只证明 `np.broadcast_to()` 合法，不创建数组；
  2. 一致性校验：lowering 得到的 `job_lookback` 必须等于编译前的
     pre_lookback 预分析值，不一致直接抛 CompileError——保证“资产轴与
     分区用的回看窗口”永远与真实 DAG 一致。

`LogicalPlan.semantic_id` 由全部 Term 的语义键与输出映射 hash 而成。物理
细节（表名、reader、load group）完全不在其中，因此同一任务切换等价物理
路径身份不变。日期轴也不在 Term 语义键中（dates 不是 TermDomain 的成员），
所以只改日期区间不会改变计划身份；但改变资产范围（codes 组成与顺序会进入
轴 fingerprint）、输入的原生 Domain、公式结构或输出映射都会改变它。

## 9. 物理规划：按写出日期切分区

代码：`execution.py` 的 `PhysicalPlanner.partitions()`。

### 9.1 做什么

`ExecutionOptions(chunk_size=3)` 下，5 个输出日期被切成两个分区：

```text
partition 0: read=[20241129, 20241202, 20241203, 20241204]
             write=[20241202, 20241203, 20241204]        slice(0, 3)
partition 1: read=[20241204, 20241205, 20241206]
             write=[20241205, 20241206]                  slice(3, 5)
```

每个分区有一个 `ReadDomain(dates, write_dates, codes, steps, output_slice)`：
`dates` 是实际读取的日期（向前扩展了 job_lookback），`write_dates` 是本分区
真正负责输出的日期，`output_slice` 是这个 chunk 将来写入最终数组的位置。

### 9.2 为什么这样设计

- **分区只切输出日期轴，不切资产或 step**。日期是唯一天然有序、可拼接的
  维度；按它切分，chunk 可以按 `output_slice` 无歧义地装配回完整数组。
- **read 与 write 分离是 lookback 的物理表达**。partition 0 从 `20241202`
  开始写出，但算这一天的 `ts_mean(close, 2)` 必须读到前一交易日，所以 read
  一直扩展到 `20241129`；partition 1 同理为 `20241205` 带入了 `20241204`。
  本例真实日历有足够历史，因此首日也有完整窗口、结果无 NaN。反过来，如果
  任务恰好从日历起点开始、无法再向前扩展，缺失窗口不会被静默补数，而是
  如实体现为 NaN（见 §11）。
- **Planner 产出的 ReadDomain 只是任务坐标起点**。`bind_many()` 会把每个
  SourceTerm 的 ReadDomain 换成它自己的原生 codes/steps（§10）——所以目标
  是分钟频时，日频 source 不会被要求返回 step 轴；目标是股票时，指数
  source 不会被要求返回股票轴。
- 统一使用任务级 `job_lookback`（而不是 per-Term 裁剪）是第一版的刻意简化：
  读取略多换来 Planner 的平凡正确性。

## 10. 执行：三阶段 Provider 契约 + 拓扑 DAG Runtime

代码：`execution.py` 的 `Runtime.execute_partition()`。

### 10.1 Source 的三个阶段

同一个逻辑 source 在任务生命周期里经过三次“形态升级”，每次回答不同的问题：

```text
describe_many   编译期     SourceRefExpr -> InputSpec
                回答：这个输入是什么 Domain/类型？（不读数据）

bind_many       分区开始   SourceTerm + ReadDomain -> SourceBinding
                回答：这个分区从哪张表、按什么坐标读？
                （此时才产生物理 SourceSpec 与 source 自己的 ReadDomain）

load_many       需要时     SourceBinding[] -> term_id -> float64 数组
                回答：真正执行 I/O，返回严格 T × N × S
```

本例中两个 source 都落在 `SmartQuant.ReturnDaily`。用户没有声明任何
load group：`bind_many()` 生成的 `load_group_key` 由物理数据集、读取参数与
读取坐标自动 hash 而成，字段级参数（`column_name`）不参与——因此同一分区
里 `ClosePrice` 和 `TurnoverVolume` 自动同组，Runtime 每个分区只调用一次
`load_many()`（最终 `stats.load_calls == 2`），在 SmartQuant 实现下就是一
条多字段 SELECT：

```text
load 事件（stats.provider_events，每分区一条）：
  dataset=SmartQuant.ReturnDaily fields=[ClosePrice, TurnoverVolume] mode=batch
  rows=8   # partition 0：4 读取日（含 lookback）× 2 资产
  rows=6   # partition 1：3 读取日 × 2 资产；两个字段一次读取
```

怎样把一次调用实现成一条 SQL 或一次 parquet scan，是 Provider 内部职责。

### 10.2 为什么这样设计

- **三个阶段分别对应三种职责**：语义描述（编译用）、物理路由（分区用）、
  I/O 执行（运行用）。Compiler 永不 load，Runtime 永不重新解释公式语义，
  两者之间只传不可变计划对象。
- **物理 SourceSpec 晚到 bind_many 才出现**，保证 §8 说的“逻辑身份不含
  物理”不只是口号；同时 Provider 可以按分区做缓存与批量合并。
- **load_many 返回什么有硬契约**：dtype 必须 float64，shape 必须精确等于
  binding 自己的 ReadDomain × 原生 N × S。缺失值统一为 NaN。Runtime 对
  契约不信任、逐一校验。

### 10.3 DAG 拓扑执行与 workspace

Runtime 按 `topological_order` 逐 Term 执行：

```text
LiteralTerm   -> 直接放入 np.float64 标量
SourceTerm    -> 所在 group 未加载则 load_many(group)，校验 dtype/shape 后入 workspace
OperatorTerm  -> 从 workspace 取输入（无名输入按位置、具名输入按关键字）
              -> OperatorSpec.func(*args, **keyword_inputs, **params)
              -> 按该 Term 自己的原生 TermDomain 校验输出 shape
              -> inf 转 NaN，写回 workspace
              -> 递减输入引用计数，归零且非输出的 Term 从 workspace 删除
```

设计要点：

- **workspace 生命周期由编译期的 `reference_counts` 驱动**。Runtime 不做
  任何 DAG 分析，只按计划记账：一个中间值在最后一个消费者完成后立即释放，
  内存峰值受控且可统计（`peak_workspace_values`）。
- **每个算子结果按它自己的原生 Domain 校验，而不是任务 shape**。
  例如 `member_mean` 直接返回 `T × 1 × S`，Runtime 就按匿名 singleton
  Domain 校验这个小数组，绝不提前物化成 `T × N × S`——“该小就小”是
  全链路一致的内存策略。
- **同一分区的多个公式共享所有中间结果**。本例 partition 0 中
  `add(close, volume)` 只计算一次，`alpha_2` 和 `alpha_3` 直接复用。

## 11. 输出边界：只做广播，不做计算

每个分区 DAG 完成后，对每个输出 Term：

```text
workspace[output_term_id]
  -> 校验 write_dates 是读取日期的连续后缀，直接截取写出区间
  -> 保留输出 Term 的原生 N/S
  -> np.broadcast_to(values, partition target shape)
  -> 数组设为只读
  -> ResultChunk(formula_id, output_slice, values)
```

本例的流消费输出（step 只有 1，数组按 T × N 展示；真实行情数值量级
悬殊——`alpha_1` 是价格/成交量量级 ~1e-7，`alpha_2` 含成交量 ~1e8，
走读只关注结构）：

```text
alpha_1 slice(0,3) shape=(3,2,1)
  [[1.167173e-07, 7.945459e-08],
   [1.056755e-07, 7.426575e-08],
   [1.138991e-07, 7.846704e-08]]
alpha_2 slice(0,3) shape=(3,2,1)
  [[1.950868e+08, 2.174827e+08],
   [2.165119e+08, 2.345631e+08],
   [2.014941e+08, 2.203473e+08]]
alpha_3 slice(0,3) shape=(3,2,1)
  [[9.754338e+07, 1.087414e+08],
   [1.082559e+08, 1.172815e+08],
   [1.007471e+08, 1.101737e+08]]
alpha_1 slice(3,5) shape=(2,2,1)  [[1.666403e-07, 1.163702e-07],
                                   [6.690729e-08, 6.610118e-08]]
alpha_2 slice(3,5) shape=(2,2,1)  [[1.374218e+08, 1.466870e+08],
                                   [3.452539e+08, 2.602072e+08]]
alpha_3 slice(3,5) shape=(2,2,1)  [[6.871089e+07, 7.334351e+07],
                                   [1.726269e+08, 1.301036e+08]]
```

设计要点：

- **输出边界只允许 `np.broadcast_to` 这一种 shape 变化**。所有真正的坐标
  变换（频率、资产映射、reduce）都必须已经是用户显式写出的算子节点；
  边界广播不插入任何物理扩展算子，也不复制数据（broadcast view 只读）。
  这让“输出 shape 为什么是这个样子”永远可以在 LogicalPlan 里找到依据。
- **chunk 是 write_dates 粒度的**。本例真实日历向前足够远，partition 0
  读到了 `20241129`，所以 `alpha_1` 第一天就有完整窗口、没有 NaN。若任务
  从日历起点开始导致回看被截断，缺失窗口不会被静默补数，而是让算子的缺失
  语义如实呈现为 NaN。
- **chunk 只包含本分区的写出区间**，形状已经是目标 shape，消费者拿到即可
  按 `output_slice` 落位。

## 12. 结果装配：compute 只是流的消费者

```text
ComputeResult
  ├── domain: ResolvedOutputDomain
  ├── arrays: formula_id -> T × N × S ndarray
  ├── plan:   LogicalPlan
  └── stats:  ExecutionStats（load_calls / peak_workspace_values /
                              released_terms / provider_events）
```

`compute()` 对每个 formula_id 延迟创建完整 float64 数组，随后完整消费
`stream()`，按 `output_slice` 写入 chunk；消费异常时清空已建数组再抛出。
本例最终（按 T × N 展示，数值四舍五入）：

```text
alpha_1 shape=(5,2,1)
  [[1.17e-07, 7.95e-08], [1.06e-07, 7.43e-08], [1.14e-07, 7.85e-08],
   [1.67e-07, 1.16e-07], [6.69e-08, 6.61e-08]]
alpha_2 shape=(5,2,1)
  [[1.95e+08, 2.17e+08], [2.17e+08, 2.35e+08], [2.01e+08, 2.20e+08],
   [1.37e+08, 1.47e+08], [3.45e+08, 2.60e+08]]
alpha_3 shape=(5,2,1)
  [[9.75e+07, 1.09e+08], [1.08e+08, 1.17e+08], [1.01e+08, 1.10e+08],
   [6.87e+07, 7.33e+07], [1.73e+08, 1.30e+08]]

stats: load_calls=2  peak_workspace_values=4
```

`stats.provider_events` 完整记录了这次任务的物理轨迹，是 review 真实
I/O 行为的入口：

```text
provider_events（按 operation 汇总）：
  catalog     建 Provider 时扫描 information_schema 与基本面 Item 清单，
              冻结任务级目录（catalog_snapshot 给出目录指纹）
  calendar    首次查询 SmartQuant.JY_TradingDayNew，之后全部 cache_hit
  asset_axis  首次查询 SmartQuant.ReturnDaily 的窗口 InnerCode，之后 cache_hit
  load        每分区一条事件：fields=[ClosePrice, TurnoverVolume]、rows=6、
              mode=batch（两个字段合并为一次物理读取）
```

设计要点：

- **compute 与 stream 不是两条计算路径**。完整结果、结果校验、临时保存
  （`TemporaryFactorRepository.save(stream)`）全部建立在“完整消费一个流”
  之上，引擎只维护一套执行语义。
- **`ResultStream` 是单次消费的 provisional 序列**：迭代器自然结束才置
  `succeeded=True`。中途拿到的 chunk 一律不能视为任务成功——这是保存、
  提交等下游操作必须完整消费流的根本原因。
- **DataFrame 不是默认形态**。`to_dataframe()` 只在显式调用时按固定布局
  `(date, asset, step) × formula_id` 构造；引擎内部永远以数组为准。

## 13. 保存的因子如何回到链路里

`load_factor("alpha")` 在 bind 后是一个普通 `SourceRefExpr("factor:alpha")`，
helper 展开、lowering、执行全程与外部 source 无差别。区别只在 Provider：
`RepositoryDataProvider` 把 `factor:` 前缀的请求路由到临时仓库的
metadata/staged npy 文件，其余委托给 base provider。设计目的是让“上次算
的因子”不需要任何新语义就能成为下次的输入——它只是一个带保存位置的
普通 Source。

## 14. 一页速查：每步的输入、输出与设计意图

| 步骤 | 输入 -> 输出 | 设计意图 |
|---|---|---|
| `from_text` | 字符串 -> Surface AST（SymbolRefExpr 占位） | 复用 Python 语法解析；parse 不做解析名称、不算数 |
| `bind` | Surface AST -> 内联展开的 AST | 作用域规则一次性强制；局部名不进入语义身份 |
| `_expand_helpers` | HelperExpr -> SourceRefExpr/OperatorExpr | 计划节点收敛为三种规范类型 |
| `describe_many` | SourceRefExpr -> InputSpec | 编译期只拿语义、不碰物理数据 |
| `_resolve_domain` | DomainSpec + InputSpecs -> ResolvedOutputDomain | 把范围冻结成唯一坐标事实 |
| `_lower` + `_intern` | 规范 AST -> Term DAG | CSE 即语义 hash；domain/lookback 逐 Term 推导 |
| `_validate_output_domain` | 输出 TermDomain vs 任务 Domain | 只确认可广播性，不创建数组 |
| `partitions` | job + chunk_size -> ReadDomain 序列 | 只切输出日期轴；read/write 分离表达回看 |
| `bind_many` | SourceTerm + ReadDomain -> SourceBinding | 物理细节最晚出现，不进计划身份 |
| `load_many` | SourceBinding -> float64 T×N×S | 契约式 I/O：dtype/shape 严格校验 |
| DAG 执行 | Term 序列 -> workspace 值 | 引用计数驱动生命周期；按原生 Domain 校验 |
| 输出边界 | workspace 值 -> ResultChunk | 只做 broadcast_to；不插物理扩展 |
| `compute`/`save` | ResultStream -> ComputeResult/落盘 | 一切结果都是“完整消费流” |

## 15. 延伸阅读

- 逐步代码级调用链与字段清单：[`调用链_最新.md`](调用链_最新.md)
- 对齐规则细节：[`频率与第三维对齐规则.md`](频率与第三维对齐规则.md)、
  [`资产轴对齐规则.md`](资产轴对齐规则.md)
- 指数/行业统计场景的专门走读：[`指数与行业统计调用链.md`](指数与行业统计调用链.md)
- 术语定义：根目录 [`CONTEXT.md`](../CONTEXT.md)
