# 统一因子计算引擎设计

- 状态：目标设计基线；按位置 ArrayLayout 改造待实现
- 日期：2026-08-30
- 适用范围：新的统一因子公式编译与计算引擎
- 术语表：[CONTEXT.md](CONTEXT.md)
- 设计讨论日志：[design.md](design.md)
- 架构决策：[ADR-0001](docs/adr/0001-use-positional-array-layout-for-operators.md)

## 1. 文档地位

本文是后续实现的权威设计基线。`design.md` 保留为设计思考日志，已有代码、旧设计文档和旧 API 只可作为经验与测试素材，不构成兼容要求，也不是未来架构必须遵守的标准。

如果实现与本文冲突，应先修改本文并说明新的设计决定，再修改代码；不通过兼容分支同时保留两套计算语义。

## 2. 目标

系统提供一套任务级因子计算能力，同时服务：

- 多组多行字符串公式的批量计算；
- 直接构造 AST 的程序化调用；
- 未来重新设计的交互式研究层；
- 未来可能接入的自动因子挖掘系统。

核心目标是：

1. 所有入口最终使用同一种 AST、编译管线和 Runtime。
2. 公式语义、数据物理位置和执行资源策略彼此分离。
3. 多公式合并为一个 Term DAG，共享 source 和公共子表达式。
4. Runtime 只执行已经编译、绑定和分区的计划。
5. NumPy 负责高效数值计算与无复制广播，Compiler 负责广播前的语义合法性。
6. 输出始终可以通过明确的 Domain 解释，不向外暴露无法解释的裸数组。
7. 第一版优先保证语义正确、实现简单和可验证，不预先建设分布式、通用存储或复杂类型系统。

## 3. 第一版非目标

以下能力不属于第一版核心引擎：

- 正式 FactorRepository 的存储格式、版本和并发协议；
- xarray 输出；
- 多进程、分布式调度和远程 worker；
- 自适应内存分块；
- per-Term 或 per-source 日期 offset 优化；
- 公式级独立失败恢复；
- 自动重试、任务恢复和取消协议；
- 跨任务中间结果缓存；
- 通用 nullable dtype 和 NumPy dtype 推导系统；
- 研究层 Registry、alias、因子生命周期和新鲜度策略；
- 自动挖掘的候选准入和评价策略；
- 通用结果回调或任意 ResultSink；
- 默认逐节点 profiling 和数组质量扫描。

第一版必须支持显式 DataFrame 转换，但不会默认把计算结果转换为 DataFrame。

## 4. 总体架构

```text
字符串公式 / 直接构造 AST
          │
          ▼
FormulaBatch Adapter / Parser
          │
          ▼
Surface AST
          │
          ▼
Compiler
  ├── SymbolBinder
  ├── HelperExpander
  ├── SourceDescriber
  ├── OutputDomainResolver
  ├── ArrayLayoutLowering
  ├── Canonicalizer / Validator
  ├── TermLowering / CSE
  └── LookbackAnalyzer
          │
          ▼
LogicalPlan：多输出 Term DAG
          │
          ▼
PhysicalPlanner
  ├── 日期分区
  ├── read/write dates
  └── 全任务最大 lookback
          │
          ▼
DataProvider.bind_many()
          │
          ▼
PhysicalPlan + SourceBindings
          │
          ▼
Runtime
  ├── 拓扑执行
  ├── load_many()
  └── Workspace 生命周期
          │
          ▼
ResultChunk → ResultStream
          ├── collect() → ComputeResult
          ├── to_dataframe()
          └── 临时 FactorRepository consumer
```

Engine Facade 统一协调上述过程。调用方不自行实现编译、source fallback、lookback 或分块循环。

## 5. 公共请求协议

### 5.1 ComputeRequest

语义请求只描述“算什么”：

```python
ComputeRequest(
    domain=DomainSpec(...),
    batch=FormulaBatch(...),
)
```

它不包含：

- chunk size；
- worker 数；
- 内存预算；
- 数据库连接；
- 输出目录；
- 是否写因子仓库。

### 5.2 ExecutionOptions

执行选项只描述“本次怎样执行”：

```python
ExecutionOptions(
    chunk_size=None,
)
```

第一版只有显式日期 `chunk_size`；`None` 表示 whole-domain 单分区执行。不同 ExecutionOptions 不得改变公式语义和最终数值。

## 6. FormulaBatch 与名称作用域

### 6.1 结构

```text
FormulaBatch
├── common_inputs：所有公式组共享、但自身不产生输出的顺序绑定程序
└── formulas：formula_id -> FormulaProgram
    ├── factor1：独立顺序绑定程序
    └── factor2：独立顺序绑定程序
```

核心结构可表达为：

```python
Binding(name, expression)

FormulaProgram(
    bindings=[Binding(...), ...],
)

FormulaBatch(
    common_inputs=FormulaProgram(...),
    formulas={
        "factor1": FormulaProgram(...),
        "factor2": FormulaProgram(...),
    },
)
```

字符串只是 Adapter 输入；Compiler 接收结构化 FormulaBatch 和 AST。

### 6.2 字符串示例

```python
batch = FormulaBatch.from_text(
    common_inputs="""
close = get_lf("stk", "ClosePrice")
vol = get_lf("stk", "Volume")
amount = get_lf("stk", "TurnoverValue")
high = get_lf("stk", "HighPrice")
""",
    formulas={
        "factor1": """
avg_price = ts_mean(close, 5)
vol_20d = ts_std(vol, 20)
intermediate = avg_price / power(vol_20d, 2)
factor = ts_corr(intermediate, amount, 10)
""",
        "factor2": """
amihud_value = amihud(close, vol, 20)
swing = ts_max(high - close, 20)
volatility = ts_std(vol, 20)
intermediate = amihud_value / swing
factor = log(volatility) + intermediate
""",
    },
)
```

### 6.3 名称规则

1. `common_inputs` 和每个公式组都是顺序绑定程序，两者都可以声明 Source、operator 表达式和中间变量。
2. `common_inputs` 可以引用自己前面已经定义的 common input，但自身不产生输出。
3. 公式组可以引用 `common_inputs` 和自己前面已经定义的局部名称。
4. 公式组不能引用其他公式组的局部名称。
5. 禁止前向引用。
6. 禁止重复定义同一作用域内的名称。
7. 禁止局部名称覆盖 common input。
8. operator 和 helper 名称是保留字，不能作为绑定名称。
9. 每个公式组至少包含一个绑定；最后一个绑定的表达式是该组输出。
10. 输出身份是外层 `formula_id`，不依赖最后一个局部变量的名称。

`common_inputs` 允许绑定任意 AST 表达式，而不只允许单个 Source；例如复权价格 helper
可以展开为多个 SourceRef 和 OperatorExpr。Source 同样可以直接声明在某个公式组内，
此时只在该公式组内可见。

多个输出需要复用同一指标时，应把指标及其共享中间变量放入 `common_inputs`：

```python
batch = FormulaBatch.from_text(
    common_inputs="""
close = get_lf("stk", "ClosePrice")
volume = get_lf("stk", "TurnoverVolume")
raw_indicator = close / volume
indicator = ts_mean(raw_indicator, 10)
""",
    formulas={
        "indicator_std_20": "factor = ts_std(indicator, 20)",
        "indicator_std_5": "factor = ts_std(indicator, 5)",
        "indicator_mean_20": "factor = ts_mean(indicator, 20)",
    },
)
```

名称绑定后，`indicator` 对应的表达式进入共享 Term DAG。多个输出复用同一个 Term，
未被任何输出引用的 common input 则不会进入 LogicalPlan 或触发数据加载。

### 6.4 名称与 CSE

左值名称和 formula ID 不参与表达式或 Term 的结构身份。因此：

- 两个公式组用不同局部名称表达相同计算时，可以共享同一个 Term；
- 两个公式组都使用 `intermediate` 但表达式不同时，不会互相影响；
- `SymbolRefExpr` 在名称绑定后消失，不进入 Canonical AST 或 Runtime。

## 7. AST 模型

### 7.1 Surface AST

Surface AST 可暂时包含：

- `LiteralExpr`：公式操作数中的字面量；
- `SymbolRefExpr`：当前作用域中的名称引用；
- `SourceRefExpr`：对外部逻辑数据的引用；
- `OperatorExpr`：已经识别的算子调用；
- `HelperExpr`：字符串解析产生的、尚待展开的 helper 调用。

Python helper 可以直接构造 SourceRefExpr、OperatorExpr 或它们的组合。字符串 Parser 产生等价的 Surface AST。

所有 AST 节点应不可变，名称绑定复用节点引用而不是深拷贝表达式。

### 7.2 Helper

helper 是纯 AST builder：

- 不读取数据；
- 不注册全局 source；
- 不修改 Registry；
- 不创建执行计划；
- 不访问 Workspace。

例如：

```python
get_lf("stk", "ClosePrice", adjusted=True)
```

可以展开为：

```text
multiply(
    SourceRefExpr(close_price),
    SourceRefExpr(adjust_factor),
)
```

helper 必须在 Canonical AST 生成前完全展开。

### 7.3 Canonical AST

完成名称绑定、helper 展开和必要的 ArrayLayout Lowering 后，Canonical AST 只包含：

- `LiteralExpr`；
- `SourceRefExpr`；
- `OperatorExpr`。

所有 delay、mask、资产投影、指数选择、广播和重采样语义在进入 Term Lowering 前必须成为显式 OperatorExpr。

### 7.4 已保存因子引用

第一版暂定：

```python
load_factor("alpha_001")
```

返回一个逻辑 SourceRefExpr，表示读取已经保存的因子。它与其他 source 使用相同的 describe、bind 和 load 协议。

`load_factor()` 不判断是否应该重新计算。如果未来研究层需要重新计算已有公式，应在提交 FormulaBatch 前把该公式 AST 显式导入；引擎不根据“数据是否存在”猜测读取还是重算。

## 8. Compiler 管线

### 8.1 唯一语义编译管线

采用以下固定顺序：

1. `Parser`：把字符串程序解析为 Binding 和 Surface AST，并保留 formula ID、行列位置。
2. `SymbolBinder`：按作用域和顺序解析 SymbolRefExpr，拒绝未知、前向和跨组引用。
3. `HelperExpander`：把所有 HelperExpr 展开为 SourceRefExpr 和 OperatorExpr。
4. `SourceDescriber`：通过 DataProvider.describe_many() 获得每个 SourceRef 的 InputSpec。
5. `OutputDomainResolver`：把 DomainSpec 解析为不可变 ResolvedOutputDomain。
6. `ArrayLayoutLowering`：只推导 Source 和 shape-changing operator 的结构维度；普通算子不比较业务坐标身份。
7. `Canonicalizer / Validator`：规范化参数并校验 operator 参数、ValueKind 和 lookback 契约，生成 Canonical AST。
8. `TermLowering / CSE`：生成 LiteralTerm、SourceTerm、OperatorTerm，并按结构身份合并公共节点。
9. `LookbackAnalyzer`：计算每个 Term 和全部输出所需的有限日期 lookback。
10. 构造多输出 LogicalPlan。

不设置一个与 Compiler 重复负责 helper、alias、mask、delay 和对齐的语义 Planner。`PhysicalPlanner` 只处理执行分区和读取范围，不修改公式语义。

### 8.2 编译产物

LogicalPlan 至少包含：

- Term DAG；
- formula ID 到输出 Term 的映射；
- 每个 Term 的 ValueSpec 和必要的 ArrayLayout；
- SourceTerm 列表；
- 拓扑顺序；
- Term 消费者计数；
- 每个 Term 和整批任务的 date lookback；
- 公式来源位置和诊断关联。

LogicalPlan 不包含数据库路径、日期 chunk 或 worker 数。

## 9. Source 语义与数据端口

### 9.1 分层

```text
SourceRefExpr
  “公式需要什么数据”
        │ describe_many
        ▼
InputSpec
  “这份数据在编译语义上是什么”
        │ Term Lowering
        ▼
SourceTerm
  “这个逻辑输入在 DAG 中的身份”
        │ bind_many
        ▼
SourceBinding
  “本次任务怎样读取这个 Term”
  ├── SourceSpec：物理上去哪里读
  ├── ReadDomain：本次读什么范围
  └── LoadGroupKey：和哪些字段一起读
        │ Reader.read
        ▼
RawBatch
        │ LoadNormalizer
        ▼
NormalizedSourceBatch
  “可以进入 Workspace 的权威 T × N × S 数组”
```

InputSpec 是 SourceRefExpr 与物理 SourceSpec 之间的语义适配契约，用于解耦编译和取数。真正把某个 SourceTerm、SourceSpec 与本次 ReadDomain 关联起来的是 SourceBinding。

### 9.2 SourceRefExpr

SourceRefExpr 只保存稳定逻辑身份和影响返回数据语义的参数，例如：

- 逻辑数据 key；
- asset type；
- 字段语义参数；
- quarters；
- 明确的 PIT/as-of 参数。

它不保存：

- 数据库地址；
- 表名；
- 文件路径；
- 连接信息；
- 本次任务读取日期。

### 9.3 InputSpec

第一版 InputSpec 至少包含：

```text
asset_type
frequency
step_spec
value_kind
```

Runtime 的 physical dtype 和 missing 约定是全局固定协议，不需要在每个 InputSpec 重复配置。DataProvider 必须在返回数组前完成规范化。

### 9.4 SourceSpec

SourceSpec 描述物理读取方式，例如：

```text
provider identity
dataset/table/path
field/column
physical query parameters
```

改变 SourceSpec 的数据库位置但保持 InputSpec 和数据产品语义不变时，公式 AST 和 LogicalPlan 的语义身份不应改变。

### 9.5 DataProvider

第一版对 Engine 只定义一个 DataProvider 端口；Provider 内部把物理 Reader 与
LoadNormalizer 分开：

```python
class DataProvider:
    def describe_many(self, source_refs) -> dict:
        ...  # SourceRefExpr -> InputSpec

    def bind_many(self, source_terms, read_domain) -> list:
        ...  # SourceBinding[]

    def load_many(self, bindings) -> dict:
        ...  # term_id -> ndarray
```

职责：

- `describe_many()` 为 Compiler 提供 source 的逻辑规格；
- `bind_many()` 为当前物理分区解析 SourceSpec、ReadDomain 和 LoadGroupKey；
- `load_many()` 编排 Reader 和 LoadNormalizer，并返回与绑定契约严格一致的数组。

Reader 只负责物理 I/O 和返回 RawBatch。LoadNormalizer 是唯一负责以下工作的组件：

- 将外部 bool、integer、NULL 和 sentinel 规范化为 Runtime 值协议；
- 对齐到 SourceBinding 指定的 dates、codes 和 steps；
- 返回完整 `T × N × S` 数组；
- 校验 shape、dtype、Infinity、ValueKind 和缺失值；
- 不执行资产投影、频率聚合或因子 operator。

Runtime 信任 NormalizedSourceBatch，不再重复扫描 Source 数组。Reader Strategy 与
RawBatch/LoadNormalizer 的详细契约见
[`docs/Reader与Load规范化边界设计.md`](docs/Reader与Load规范化边界设计.md)。

### 9.6 LoadGroup

相同物理数据集、兼容查询语义和相同 ReadDomain 的多个字段应组成一个 LoadGroup。例如 ClosePrice 和 Volume 位于同一张表时，`load_many()` 应尽量使用一次物理查询读取两个字段。

第一版可以为不支持批量查询的后端保留逐字段 fallback，但 Runtime 仍只调用 `load_many()`，不自行搜索或读取 source。

同组任何必要字段读取或校验失败时，第一版整个任务 fail-fast。

## 10. Domain 模型

### 10.1 DomainSpec

```python
DomainSpec(
    start=...,
    end=...,
    asset_scope={...},
    target_asset=...,
    target_freq=...,
    target_step_count=...,
)
```

- `start/end` 是调用方需要的输出日期范围，均包含端点；
- `asset_scope` 声明本任务允许使用的资产类型和资产集合；
- `target_asset` 决定输出资产类型；
- `target_freq` 决定输出采样频率；
- `target_step_count` 独立决定输出第三维长度，二者不能互相推导。

`target_asset` 必须存在于 AssetScope。公式引用未在 AssetScope 声明的资产类型或资产代码时编译失败。

### 10.2 AssetScope

示例：

```python
asset_scope={
    "stk": "all",
    "idx": ["000300", "000905"],
}
```

第一版定义：

- `"all"` 表示任务开始时 DomainCatalog 当前快照为该资产类型提供的完整、有序 master axis；
- 该轴不依赖本次 start/end 内哪些资产处于活跃状态；
- 任务执行期间轴不可变；
- 后续任务可以因 DomainCatalog 更新而解析到新的 master axis；
- 尚未上市、已经退市、停牌、指数成分和其他随日期变化的状态通过 mask/source 表达；
- 显式代码子集保留调用方顺序，拒绝重复和未知代码。

未来如果需要“请求区间内出现过的资产并集”，应增加明确的选择器，例如 `ActiveDuring(...)`，不改变 `"all"` 的含义。

### 10.3 两类权威 Domain 与 ArrayLayout

#### OutputDomain

用户要求返回的精确坐标：

```text
dates
target asset type
target codes
target frequency
target steps
calendar identity
axis fingerprint
```

#### ReadDomain

PhysicalPlanner 为某个分区生成的读取范围：

```text
read dates
write dates / output slice
ResolvedOutputDomain reference
```

输出范围和读取范围严格分离。单日输出可以因 rolling 或 delay 读取此前多日。

#### ArrayLayout

普通算子中间值不再具有权威坐标 Domain，只保留运行所需的结构布局：

```text
T：当前物理分区日期长度
N：第二维长度
S：第三维长度
```

ArrayLayout 不保存资产类型、代码顺序、calendar 或 axis fingerprint，也不承诺两个
相同位置具有相同业务含义。Source 的原生资产、频率和 step 信息仍由 InputSpec 与
SourceBinding 保存，用于取数和确需元数据的显式转换算子。

### 10.4 日期与日历

第一版：

- 由 `target_asset/target_freq` 通过 DomainCatalog 推导 OutputDomain 的唯一交易日历；
- `start/end` 解析为该日历上的有序输出 dates，lookback 也按其 session 数计算；
- SourceBinding 把每个 source 重建到共同 ReadDomain 日期轴，不存在的记录成为 Missing；
- source 自身的 calendar 身份不参与普通算子兼容性检查；只有 Provider 无法把物理日期
  映射到 ReadDomain 时才在 bind/load 边界失败；
- 如果输出日历历史不足，读取可获得的最大前缀，rolling/min_periods 决定输出是否为 NaN，而不是伪造日期。

## 11. ArrayLayout 与 NumPy 广播

### 11.1 原则

普通 operator 只按位置处理 ndarray。Compiler 和 Runtime 不比较输入的资产类型、
代码顺序、frequency、calendar 或 axis fingerprint；shape 相同即允许按位置计算，
业务对齐正确性由公式作者和数据产品契约负责。

### 11.2 可以使用 NumPy 自动广播的情况

1. scalar literal 与任意数组；
2. 日期维必须等于当前分区的 T，不允许用日期 singleton 猜测业务含义；
3. N 和 S 分别只要求相等或一侧为 1；
4. 最终输出的 N/S 也可以从 singleton 广播到 OutputDomain 请求的长度。

实现直接复用 `np.broadcast_shapes()` / `np.broadcast_to()`，不维护另一套手写广播算法。

### 11.3 对齐规则

SourceTerm 仍保留 DataProvider 描述的原始 frequency 和 step_count，但普通算子不会据此
拒绝输入。标准分钟频通常因 S 不同而自然无法广播；如果不同频率或不同业务 step
恰好具有相同 S，则允许直接按位置计算。

改变物理 layout 的操作仍必须显式：

- 粗日内到细日内使用 `align_frequency(..., method="ffill")`；
- 细日内到粗频率或日频使用 `resample(..., method=...)`；
- 资产选择、资产投影、step 选择和 reduce 使用对应显式 operator/helper；
- shape-changing operator 只声明有限的 layout effect，例如保留、N 归约为 1、S 归约为 1；
- `resample` 等需要源频率或坐标元数据的算子保留专属 lowering；当输入来源无法提供
  唯一元数据时，调用方必须显式补充参数或编译失败。

这些显式操作用于得到需要的数组布局或业务结果，不构成普通算子输入必须同坐标的限制。
delay、mask 和 PIT 政策仍必须由 helper 或公式显式产生。

### 11.4 指数与转债映射决策

三种业务操作共用现有 Source、helper 和通用 operator，不建立统一的
“资产映射框架”：

| 操作 | Source 注册 | helper 展开 | 原生输出 |
| --- | --- | --- | --- |
| 选择单个指数特征 | 用户显式声明指数 Source | `select_index_feature` 降为位置选择 | `T × 1 × S` |
| 指数成分池统计 | 用户显式声明股票特征和成分 Source | `index_member_stat` 降为 `member_*` | `T × 1 × S` |
| 股票投影到转债 | helper 自动注入任务级关系 Source | `project_stk_to_cb` 降为 `lookup_by_col` | `T × N_cb × S` |

指数两种操作不隐式生成 mapping，也不把 singleton 物理扩展到股票或转债轴；
后续普通运算由 NumPy 按 shape 广播，如果只希望保留指数成分，公式显式使用 mask。
股票到转债必须将原始正股 InnerCode 换成当前任务股票轴上的位置，
该位置列由任务级 Provider 生成，其他层不猜测或缓存跨任务列号。

详细 API、调用链和边界见
[`docs/资产轴对齐规则.md`](docs/资产轴对齐规则.md)。

## 12. Runtime 值协议

### 12.1 物理表示

第一版所有 Runtime 张量统一使用：

```text
physical dtype = np.float64
missing = np.nan
```

逻辑 ValueKind：

```text
numeric
mask
code
```

- mask：`1.0=True`、`0.0=False`、`NaN=Missing`；
- code：有限整数值使用 float64 表示，`NaN=Missing`；
- 普通非 literal Term 的 shape 为 `T × N × S`；
- literal 可以是 scalar，由 NumPy 在通过语义校验后广播。

ValueKind 只描述有限值的逻辑集合，不负责执行数组转换。Source Load 规范化边界先把
正负 Infinity 转换为 NaN，再校验 MASK 的 `0/1/NaN` 和 CODE 的整数/NaN。内置
operator kernel 必须保证不返回 Infinity；Runtime 不再为每个中间 Term 重复全数组扫描。

### 12.2 缺失和三值逻辑

- 任一比较输入 Missing 时结果为 Missing；
- `not(Missing) = Missing`；
- AND：任一输入 False 则 False；否则任一 Missing 则 Missing；否则 True；
- OR：任一输入 True 则 True；否则任一 Missing 则 Missing；否则 False；
- `where(mask, x, y)` 在 mask 为 Missing 时输出 Missing；
- `apply_mask(x, mask)` 只在 mask 为 True 时保留输入，False 或 Missing 均输出 Missing；
- 截面 `sample_mask` 的 Missing 不参与统计样本，按 False 的选择语义处理；输出位置是否保留由各截面算子对 False sample 的既有契约决定，不因 Missing 另行传播；
- `member_*` 的成员 mask 为 Missing 时按非成员处理；
- 数值算子产生的非法或非有限结果由 kernel 直接写为 NaN；最终输出边界保留一次兜底规范化。

## 13. Operator 协议

第一版 OperatorSpec 只保留必要字段：

```text
name
func
input_kinds
output_kind
date_lookback
layout_effect
```

- `input_kinds` 同时表达输入数量与 ValueKind；
- `output_kind` 是固定 ValueKind 或继承指定输入；
- `date_lookback` 是非负整数或只依赖编译期字面量参数的纯函数；
- `layout_effect` 只允许有限的结构变化（保持、N 归约、S 归约等），不接收或比较业务坐标；
- `resample`、频率对齐和资产选择等确需元数据的算子使用专属 lowering；
- 参数先通过 Python 函数签名排除未知或缺失参数，业务范围由算子自己校验。

第一版注册的 operator 必须：

- 确定；
- 无副作用；
- 不原地修改输入；
- 对相同输入产生相同输出；
- 声明有限日期 lookback；
- 遵守 Runtime dtype、missing、ValueKind 和 shape 契约。

只有满足这些条件，OperatorTerm 才能安全进行结构 CSE 和 Workspace 引用计数释放。

## 14. Term DAG 与 LogicalPlan

### 14.1 Term 类型

```text
Term
├── LiteralTerm
├── SourceTerm
└── OperatorTerm
```

- LiteralTerm：公式操作数中的小型不可变字面量；
- SourceTerm：带 SourceRef 和 InputSpec 的外部逻辑输入；
- OperatorTerm：调用 OperatorSpec，并引用其他 Term。

SymbolRef、helper、绑定名称和物理 SourceSpec 都不会成为 Term。

### 14.2 结构身份和 CSE

结构身份至少包含：

```text
LiteralTerm:
  normalized literal + ValueSpec

SourceTerm:
  SourceRef semantic identity + InputSpec + Source ArrayLayout

OperatorTerm:
  operator name + dependency term ids
  + normalized params + output ValueSpec
```

以下内容不参与身份：

- 局部变量名；
- formula ID；
- 数据库地址或表路径；
- chunk size；
- Runtime worker 配置。

相同结构节点在整个 FormulaBatch 内自动合并。

### 14.3 Lookback

```text
term_lookback(term) =
    local_operator_lookback
    + max(dependency_lookback)

job_lookback = max(output_term_lookback)
```

第一版所有分区和 SourceTerm 统一使用 job_lookback，不实现 per-Term offset。

影响日期 lookback 的参数必须在编译期可知；无界日期窗口第一版拒绝。

第一版 Runtime 的 `axis` 参数只接受 `0=date`、`1=asset`、`2=step`，不支持负 axis。`delay/step_delay/step_diff/step_pct_change` 的 `periods` 必须是非负整数；负 periods 表示未来依赖，在尚未建模 future read horizon 前必须于编译期拒绝。

## 15. PhysicalPlan 与分块

### 15.1 分区

PhysicalPlanner 根据 ResolvedOutputDomain 和 ExecutionOptions 生成有序日期分区。

每个分区包含：

```text
partition_id
output_slice
write_dates
read_dates
```

其中：

```text
read_dates = write_dates + 之前最多 job_lookback 个交易 session
```

### 15.2 第一版执行策略

- 单进程；
- 单线程任务级 Scheduler；
- 分区顺序串行；
- `chunk_size=None` 时 whole-domain；
- 每个分区使用新的 Workspace；
- 每个分区统一读取最大 ReadDomain；
- Runtime 只输出 write dates；
- 不实现重试、worker 并行或自适应资源规划。

## 16. Runtime 与 Workspace

Runtime 接收已经完成 source binding 的 PhysicalPlan 分区，不再编译公式或搜索数据位置。

执行算法：

1. 按 LogicalPlan 拓扑顺序遍历 Term；
2. 遇到 SourceTerm 时，通过其 LoadGroup 调用 DataProvider.load_many()；
3. LiteralTerm 直接产生标量；
4. OperatorTerm 从 Workspace 取依赖并调用 OperatorSpec.func；
5. 信任内置 operator 契约，不执行逐 Term 业务坐标或全数组重复校验；
6. 依赖的剩余消费者计数递减；
7. 某个非输出 Term 不再被任何节点引用时，从 Workspace 删除；
8. 公式输出节点就绪后生成 ResultChunk；
9. 当前分区结束后释放 Workspace 中剩余非必要引用。

Runtime 不访问：

- FormulaBatch 的局部名称；
- helper；
- DomainSpec；
- 数据库路由规则；
- 因子仓库目录；
- DataFrame。

## 17. 输出协议

### 17.1 ResultChunk

```python
ResultChunk(
    formula_id=...,
    output_slice=...,
    values=...,  # write_dates × target assets × target steps
)
```

- `output_slice` 是 ResolvedOutputDomain 日期轴上的整数 slice；
- codes、steps 和完整 dates 不在每个 chunk 重复保存；
- values shape 必须与 domain 和 output_slice 一致；
- Runtime 在交付后不再修改 values；
- ResultChunk 只包含公式输出，不暴露普通 DAG 中间节点。

### 17.2 ResultStream

```text
ResultStream
├── domain: ResolvedOutputDomain
└── ResultChunk iterator
```

第一版 ResultStream：

- 是同步、单次消费的有序流；
- 按日期分区顺序输出，每个分区内按 FormulaBatch 公式顺序输出；
- 只在自然遍历结束时表示任务成功；
- 在整个流成功结束前，所有已产生 chunk 都是 provisional；
- 中途异常、提前关闭或未完整消费均不表示成功；
- 第一版任意 Parse、Compile、Domain、DataProvider 或 Runtime 错误都会 fail-fast。

### 17.3 Engine API

```python
stream = engine.stream(request, options=ExecutionOptions(...))
result = engine.compute(request, options=ExecutionOptions(...))
```

`engine.compute()` 只能通过完整消费 `engine.stream()` 实现，不建立第二条执行路径。

### 17.4 ComputeResult

```python
ComputeResult(
    domain=resolved_output_domain,
    arrays={
        "factor1": ndarray,
        "factor2": ndarray,
    },
)
```

- 每个数组 shape 为完整 `date × asset × step`；
- 所有数组共享顶层 domain；
- 发生错误时，collector 丢弃所有部分装配结果并重新抛出异常；
- 不返回部分成功的 ComputeResult。

### 17.5 DataFrame

第一版必须提供显式转换：

```python
df = result.to_dataframe()
```

默认契约：

```text
index   = MultiIndex(date, asset, step)
columns = FormulaBatch 中的 formula_id
values  = 对应公式结果
```

日频 `step=1` 仍保留 step 索引层，保证日频与日内使用同一种结构。转换必须显式调用，因为高频完整数组转换为 DataFrame 可能产生很大的索引和内存开销。

第一版不提供多种 wide/long 格式开关；调用方需要其他布局时使用普通 pandas 操作转换。

## 18. 临时 FactorRepository 边界

正式 FactorRepository 暂不设计。第一版如需验证保存与 `load_factor()` 闭环，只实现最小临时 consumer/provider：

- consumer 将 ResultChunk 写入 staging；
- ResultStream 正常结束后提交；
- 流异常或提前结束时删除 staging；
- provider 通过 DataProvider 协议 describe、bind、load；
- 保存时必须同时保存足以恢复 Domain 的 dates、codes、steps 和公式输出身份。

临时实现不确定未来正式仓库的文件布局、固定资产轴、增量 upsert、并发或版本协议，不允许这些实现细节进入 Engine、AST、Term 或 ResultStream。

## 19. 错误与诊断

第一版采用 fail-fast，但错误必须能定位阶段和公式。

建议的错误类别：

```text
ParseError
SymbolBindingError
CompileError
DomainError
DataProviderError
RuntimeExecutionError
ResultAssemblyError
```

在信息可得时，错误至少携带：

- stage；
- formula ID；
- source span（行、列）；
- operator 或 source logical key；
- 原始 cause。

以下情况是错误：

- 未知、前向或跨公式组名称引用；
- 重复名称或保留字冲突；
- helper/operator 不存在；
- operator 参数不合法；
- source 无法 describe 或 bind；
- AssetScope 缺少所需资产类型；
- 无法唯一 Lower 的资产或频率转换；
- provider 返回错误 dtype、shape 或坐标；
- operator 违反输出契约。

按照协议产生的 NaN、窗口历史不足、无效数学输入转 NaN，不属于执行失败。

## 20. 第一纵向实现切片

第一阶段只实现足以验证完整主链的最小能力：

```text
多组顺序公式
Literal/Source/Operator AST
SymbolBinder 和 helper 展开
Source/Output/Read Domain
按位置 ArrayLayout 与 NumPy singleton 广播
float64 + NaN
内存 DataProvider
Term DAG、CSE、拓扑执行
有限 lookback
whole-domain 和固定日期 chunk
ResultStream、ComputeResult、DataFrame
单进程、fail-fast
```

不因后续跨资产、真实数据库或正式存储需求增加第一阶段抽象。

## 21. 建议实施顺序

### M0：AST、Parser 与 FormulaBatch

- 定义不可变 AST；
- 实现多行赋值 Parser；
- 实现 FormulaBatch Adapter；
- 实现作用域和 SymbolBinder；
- 固定 source span 诊断。

### M1：Compiler 与 Term DAG

- helper 展开；
- 内存 DataProvider.describe_many()；
- ResolvedOutputDomain 和 Source ArrayLayout；
- Canonical AST；
- Term Lowering、结构身份和 CSE；
- lookback 分析。

### M2：Runtime 与输出

- 拓扑执行和 Workspace 引用计数；
- whole-domain ResultStream；
- collect() 与 ComputeResult；
- DataFrame 转换；
- fail-fast 清理。

### M3：日期分块

- PhysicalPlanner；
- job-wide 最大 lookback；
- read/write dates；
- whole-domain 与 chunk 结果等价验证。

### M4：真实 DataProvider

- SourceSpec 和 SourceBinding；
- LoadGroup；
- 同表多字段 load_many()；
- 坐标、dtype、shape 和 missing 校验。

### M5：显式 Layout 转换

- 日频到日内 singleton step 广播；
- 频率投影和显式 resample；
- `stk → cb`；
- 指数选择和广播；
- 其他已确认对齐规则。

### M6：临时因子保存闭环

- provisional chunk staging；
- 流完成 commit、失败 abort；
- `load_factor()` 通过 DataProvider 重新读取。

## 22. 验收标准

第一版至少覆盖：

1. 两个公式组使用相同表达式但不同局部名称时，生成同一个共享 Term。
2. 两个公式组使用相同局部名称但不同表达式时，作用域互不影响。
3. 前向引用、跨组引用和未知名称产生带 formula ID、行列位置的错误。
4. helper 返回 AST，不修改全局状态或触发读取。
5. SourceRef 的物理路由变化不改变公式和 LogicalPlan 语义身份。
6. 单日输出配合 20 日 rolling 时，读取范围正确向前扩展。
7. whole-domain 和日期 chunk 结果逐元素一致，包括 NaN 位置。
8. 相同 LoadGroup 的多个字段通过一次 load_many() 读取。
9. provider 返回错序 codes、错误 shape 或错误 dtype 时被拒绝。
10. NumPy singleton 广播不产生不必要的完整复制。
11. shape 相同但坐标身份不同的普通 Term 按位置计算；测试必须明确锁定这一契约。
12. Workspace 在最后一个消费者结束后释放非输出 Term。
13. ResultChunk shape 与 OutputDomain slice 一致。
14. ResultStream 后续失败时，先前 chunk 不被视为已提交结果。
15. `engine.compute()` 只通过消费 ResultStream 实现。
16. ComputeResult 数组和 DataFrame 都能由同一 ResolvedOutputDomain 无歧义解释。
17. DataFrame 使用 `(date, asset, step)` MultiIndex 和 formula ID 列。

## 23. 明确延后的演进点

只有真实需求或 profiling 证明必要时，才考虑：

- 正式 FactorRepository 与 Zarr/Parquet 等存储选择；
- AssetScope 的 `ActiveDuring(...)` 等动态轴选择器；
- per-source/per-Term lookback 和 offset；
- 多进程 RuntimeBackend；
- 内存预算驱动的自动分区；
- 独立公式失败传播与继续执行；
- RuntimeReport 和 operator profiling；
- 多物理 dtype、validity bitmap 和紧凑 mask/code；
- 跨任务计划或 Term 缓存；
- 多日历批次；
- xarray 适配；
- 正式研究平台和因子 Registry。

这些能力不得通过提前增加空接口、factory 或兼容分支进入第一版实现。
