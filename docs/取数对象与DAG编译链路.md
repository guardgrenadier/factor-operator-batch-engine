# 取数对象与 DAG 编译链路

本文说明新 batch pipeline 从“定义公共输入和公式”到“生成共享 LogicalPlan DAG”之间，所有取数相关对象、方法和元数据如何流转，以及这套分层背后的设计思路。

本文以当前代码为准，只讨论：

```text
FormulaBatch / ComputeRequest
  -> 公式解析与符号绑定
  -> helper 展开
  -> Source 元数据描述
  -> Domain 解析
  -> SourceTerm / OperatorTerm lowering
  -> LogicalPlan DAG
```

物理分区、`bind_many()`、`load_many()` 和数组计算不属于 DAG 编译阶段，但本文会说明它们与编译期对象的边界，避免把“逻辑取数引用”和“物理读取描述”混为一谈。

相关实现主要位于：

- [`formula.py`](../src/factor_engine/formula.py)：公式 AST、Parser、公共输入、名称绑定和取数 helper；
- [`compiler.py`](../src/factor_engine/compiler.py)：helper 展开、Source 描述、Domain 解析和 DAG lowering；
- [`model.py`](../src/factor_engine/model.py)：DataProvider、Domain、Term、LogicalPlan 等核心契约；
- [`providers.py`](../src/factor_engine/providers.py)：MemoryDataProvider；
- [`data_provider/`](../src/factor_engine/data_provider/)：正式 Catalog、Backend、批量读取、规范化和 SmartQuant Provider；
- [`execution.py`](../src/factor_engine/execution.py)：编译入口以及 DAG 之后的物理规划和执行；
- [`model.py`](../src/factor_engine/model.py)：物理来源对象 `SourceSpec`；
- [`legacy/data/router.py`](../src/factor_engine/legacy/data/router.py)：legacy 研究层的物理来源解析和数据路由，不进入新管线。

## 1. 总览：一条 Source 在编译期的流转

以下示例定义两个公共输入，再在两个公式中复用它们：

```python
batch = FormulaBatch.from_text(
    common_inputs="""
        close = get_lf("stk", "ClosePrice", adjusted=True)
        volume = get_lf("stk", "TurnoverVolume")
    """,
    formulas={
        "alpha_1": "factor = ts_mean(close, 5) / volume",
        "alpha_2": "factor = close / volume",
    },
)

request = ComputeRequest(
    domain=DomainSpec(
        start="20240101",
        end="20241231",
        asset_scope={"stk": "all"},
        target_asset="stk",
        target_freq="1d",
        target_step_count=1,
    ),
    batch=batch,
)

job = engine.compile(request)
```

以 `close` 为例，其编译期形态依次为：

```text
公式文本
  get_lf("stk", "ClosePrice", adjusted=True)
        |
        v
HelperExpr(
  name="get_lf",
  args=("stk", "ClosePrice"),
  params=(("adjusted", True),),
)
        |
        | Compiler._expand_helpers()
        v
SourceRefExpr(
  logical_key="stk.1d.ClosePrice",
  semantic_params=(("adjusted", True),),
)
        |
        | DataProvider.describe_many()
        v
InputSpec(
  asset_type="stk",
  frequency="1d",
  step_count=1,
  value_kind=NUMERIC,
  calendar="default",
)
        |
        | Compiler._lower_source()
        v
SourceTerm(
  term_id=...,
  source_ref=...,
  input_spec=...,
  domain=TermDomain(...),
  semantic_key=...,
)
        |
        v
LogicalPlan.terms + LogicalPlan.topological_order
```

这条链中没有物理表名、SQL、parquet 路径、加载组或实际 ndarray。DAG 只记录“要什么数据”和“该数据具有什么静态坐标契约”。

## 2. 取数对象分层

取数相关对象分成逻辑表达、静态描述、逻辑计划和物理读取四层：

| 对象 | 产生阶段 | 核心含义 | 是否进入 LogicalPlan | 是否包含物理位置 |
| --- | --- | --- | --- | --- |
| `HelperExpr` | 文本解析 | 用户层取数便利语法 | 否，编译前展开 | 否 |
| `SourceRefExpr` | helper 展开或 Python helper | 稳定的逻辑数据引用 | 通过 `SourceTerm` 保留 | 否 |
| `InputSpec` | `describe_many()` | Source 的编译期静态契约 | 通过 `SourceTerm` 保留 | 否 |
| `TermDomain` | `_lower_source()` | 本次任务内 Source 的原生坐标身份 | 是 | 否 |
| `SourceTerm` | `_lower_source()` | DAG 中可被算子依赖的数据叶子 | 是 | 否 |
| `ResolvedOutputDomain` | `_resolve_domain()` | 本次任务最终输出的冻结坐标 | 位于 `CompiledJob` | 否 |
| `ReadDomain` | `PhysicalPlanner` | 某个运行分区实际读取和写出的坐标 | 否 | 只有坐标，没有来源 |
| `SourceSpec` | `bind_many()` | 表、字段、Reader 和读取参数等物理来源 | 否 | 是 |
| `SourceBinding` | `bind_many()` | 将一个 `SourceTerm` 绑定到物理来源和分区坐标 | 否 | 是 |

最重要的分界是：

```text
SourceRefExpr + InputSpec + TermDomain
  = 逻辑语义与编译期契约

SourceSpec + ReadDomain + SourceBinding
  = 某次执行中的物理读取决策
```

同一逻辑公式可以在不同数据库、表、文件路径或加载组上执行，只要 Provider 返回的静态输入契约一致，LogicalPlan 的计算语义就不应变化。

## 3. 阶段一：定义 FormulaBatch 和 ComputeRequest

### 3.1 FormulaBatch 的两个程序层级

`FormulaBatch` 包含：

- 一个共享的 `common_inputs: FormulaProgram`；
- 多个按 `formula_id` 隔离的 `formulas: Mapping[str, FormulaProgram]`。

`common_inputs` 是所有公式共享的顺序绑定。例如：

```python
common_inputs="""
    close = source("stk.1d.close")
    volume = source("stk.1d.volume")
"""
```

这里的 `close` 和 `volume` 只是公式作用域内的符号名，不是数据目录键，也不会进入 Source 的语义身份。真正的数据身份来自 `SourceRefExpr.logical_key` 和 `semantic_params`。

每个公式也是顺序绑定程序，当前以最后一个 binding 作为该 `formula_id` 的输出：

```python
formulas={
    "alpha": """
        mean = ts_mean(close, 5)
        factor = mean / volume
    """
}
```

### 3.2 DomainSpec 定义任务边界，不定义 Source

`ComputeRequest` 将 `FormulaBatch` 与 `DomainSpec` 组合起来。`DomainSpec` 声明：

- 输出起止日期；
- 本任务允许使用的资产类型和代码范围；
- 最终输出资产类型；
- 最终输出 `frequency`；
- 最终输出 `target_step_count`。

`DomainSpec` 不是取数列表，也不要求所有 Source 与输出频率相同。每个 Source 保留 Provider 描述的原生 `frequency + step_count`，只有显式 alignment operator 或允许的 singleton 广播可以改变或合并 Domain。

## 4. 阶段二：公式文本转换为不可变 AST

### 4.1 文本入口

`FormulaBatch.from_text()` 为 `common_inputs` 和每个公式复用同一个 `FormulaParser`：

```text
FormulaBatch.from_text()
  -> FormulaParser.parse_program(common_inputs)
  -> FormulaParser.parse_program(formula_1)
  -> FormulaParser.parse_program(formula_2)
  -> FormulaBatch
```

`FormulaParser.parse_program()` 使用 Python `ast.parse()`，但只接受受限的简单赋值语法。每一个赋值生成 `Binding(name, expression, span)`，整个文本生成 `FormulaProgram(bindings)`。

### 4.2 取数调用先解析成 HelperExpr

字符串中的以下名称属于 `DEFAULT_HELPERS`：

```text
source
get_lf
get_hf
get_fund
load_factor
select_asset
select_index_feature
index_member_stat
project_stk_to_cb
```

Parser 遇到这些调用时生成 `HelperExpr`，而不是立即决定 Source key 或访问 Provider。例如：

```python
close = get_hf("stk", "5min", "ClosePrice", adjusted=True)
```

解析结果表达的是：

```text
HelperExpr("get_hf", literal args, semantic params)
```

这一阶段只做语法分类。它不检查数据是否存在，不解析表名，也不知道资产主轴和 step 数。

### 4.3 Python helper 直接产生不可变 Expr

`formula.py` 也提供 Python helper：

```python
source("stk.1d.close")
get_lf("stk", "ClosePrice")
get_hf("stk", "5min", "ClosePrice")
get_fund("stk", "NetProfit", quarters=8)
load_factor("alpha_001")
```

除带重采样参数的 `get_hf()` 返回公开 `OperatorExpr("resample")` 外，这些函数直接返回
不可变 `SourceRefExpr`；它们都不会读取数据或访问全局 Registry。当前项目尚缺少完整易用
的 Python FormulaBatch Builder，因此直接 Python AST 入口仍需要调用方自行组装 `Binding`
和 `FormulaProgram`；该补全工作记录在 [`todo.md`](todo.md)。

## 5. 阶段三：符号绑定，只保留输出真正依赖的表达式

`BatchFactorEngine.compile()` 创建 `Compiler`，随后 `Compiler.compile()` 首先调用：

```python
bound = request.batch.bind(
    reserved_names=set(self.operators) | DEFAULT_HELPERS
)
```

`FormulaBatch.bind()` 分两步工作：

1. 按声明顺序绑定 `common_inputs`；
2. 将公共环境复制为每个公式的初始环境，再绑定各自的局部变量。

`_resolve_symbols()` 会把 `SymbolRefExpr("close")` 替换成 `close` 对应的完整表达式树，因此绑定完成后，输出表达式不再依赖字符串名称查表。

该阶段拒绝：

- 前向引用；
- 跨公式访问其他公式的局部变量；
- 覆盖公共 input；
- 重复 binding；
- 使用 helper 或 operator 保留名作为 binding 名。

Compiler 后续只读取 `bound.outputs`，并从输出表达式递归寻找 Source。这意味着未被任何输出引用的公共 input 不会进入 `describe_many()`，也不会进入 DAG，形成自然的 dead-input elimination。

## 6. 阶段四：展开取数 helper，形成 Canonical AST

`Compiler._expand_helpers()` 递归展开 `HelperExpr`。对普通取数 helper，结果如下：

| 用户调用 | 展开后的 `SourceRefExpr.logical_key` |
| --- | --- |
| `source("stk.1d.close")` | `stk.1d.close` |
| `get_lf("stk", "ClosePrice")` | `stk.1d.ClosePrice` |
| `get_hf("stk", "5min", "ClosePrice")` | `stk.5min.ClosePrice` |
| `get_fund("stk", "NetProfit", quarters=8)` | `stk.1d.NetProfit` |
| `load_factor("alpha_001")` | `factor:alpha_001` |

除数据表达式外，helper 的位置参数必须是字面量。关键字参数会成为 `SourceRefExpr.semantic_params`，例如复权方式、PIT 参数或 `quarters`。

`SourceRefExpr.create()` 会排序并冻结这些参数，使 Source 引用：

- 不可变且可哈希；
- 不受调用方后续修改原容器影响；
- 不因关键字书写顺序不同而产生不同身份。

helper 展开完成后，Canonical AST 只应包含：

```text
LiteralExpr
SourceRefExpr
OperatorExpr
```

`HelperExpr` 不会进入 LogicalPlan。

### 6.1 并非所有 helper 都是取数 helper

`align_frequency()` 是默认 Registry 中的公开 `OperatorExpr`。资产选择和指数成员统计在 Parser 层属于 `HelperExpr`，但也不会展开为单独 Source：

- `select_asset()` 展开为内部资产位置选择表达式；
- `select_index_feature()` 把用户声明的指数表达式展开为内部资产位置选择；
- `index_member_stat()` 按 method 展开为已有 `member_mean/sum/std` operator。

因此“Parser helper”是语法层分类，不等于“数据加载 helper”。

`project_stk_to_cb()` 是明确的例外：它把用户层的一参数调用展开为普通
operator 和隐式关系 Source：

```text
project_stk_to_cb(values)
  -> lookup_by_col(
       values,
       SourceRefExpr("cb.1d.underlying_stk"),
     )
```

因此 mapping Source 会与用户显式声明的 Source 一起进入 `_source_refs()`、
`describe_many()` 和后续 binding/loading。它的列号由任务级 Provider 基于当前
`stk.codes` 生成，而不是 Compiler 根据静态 Catalog 推导。

`resample()` 不是 helper，而是默认 Registry 中的公开 operator。直接调用与
`get_hf(..., resample=..., method=...)` 简写都会规范化为：

```text
原始频率 SourceRefExpr
  -> 公开 resample OperatorExpr
  -> 公开 resample OperatorTerm
```

Provider 仍描述并读取原始频率 Source。Compiler 的 `_lower_resample()` 根据输入 Domain
校验细粗关系、推导输出 Domain，并补充 Runtime 所需的原始频率；默认 Operator Registry
中唯一的 `resample` kernel 执行分组聚合。该专属 lowering 不改变其公开算子身份。

## 7. 阶段五：收集和批量描述逻辑 Source

### 7.1 `_source_refs()` 收集 SourceRefExpr

helper 全部展开后，Compiler 从所有输出递归遍历：

```python
refs = tuple(dict.fromkeys(_source_refs(outputs.values())))
```

这里同时完成两件事：

- 只收集输出真正可达的 Source；
- 按首次出现顺序对等价 `SourceRefExpr` 去重。

相同逻辑键但不同语义参数是不同 Source 引用。例如：

```text
source("stk.1d.close", adjusted=True)
source("stk.1d.close", adjusted=False)
```

二者不能因为 `logical_key` 相同而合并。

### 7.2 `describe_many()` 只返回静态 InputSpec

Compiler 一次调用：

```python
input_specs = provider.describe_many(refs)
```

DataProvider 必须为每个 `SourceRefExpr` 精确返回一个 `InputSpec`：

```python
InputSpec(
    asset_type=...,
    frequency=...,
    step_count=...,
    value_kind=...,
    calendar=...,
)
```

`InputSpec` 回答的是“这个 Source 在编译期是什么”，而不是“从哪里读取”：

- `asset_type`：Source 原生资产轴类型；
- `frequency`：原生频率；
- `step_count`：第三维长度；
- `value_kind`：`NUMERIC`、`MASK` 或 `CODE`；
- `calendar`：日期坐标所属日历。

Compiler 会检查 `describe_many()` 是否遗漏 Source。此时不调用 `load_many()`，因此未知数据、非法 Source 参数或不完整元数据应尽量在描述阶段 fail-fast，而不是等到 Runtime 才失败。

### 7.3 当前 Provider 的描述方式

`MemoryDataProvider.describe_many()`：

- 优先使用显式配置的 `InputSpec`；
- 否则从规范逻辑键和内存数组第三维推导契约；
- 同时确认逻辑 Source 存在。

`RepositoryDataProvider.describe_many()`：

- 将 `factor:` 前缀引用交给临时因子仓库元数据；
- 其他 Source 批量委托给基础 Provider；
- 最终仍合并成统一的 `SourceRefExpr -> InputSpec` 映射。

`SmartQuantDataProvider.describe_many()`：

- 从任务级 `ResolvedCatalog` 返回逻辑 `InputSpec`；
- CatalogEntry 不保存或产生 `SourceSpec`；
- 只有之后的 `bind_many()` 才把 dataset、table、field 和物理参数组装为 `SourceSpec`；
- 整条正式链不依赖 Store、Router、旧 Reader 或 FeatureArray。

编译阶段不做任何物理路由。`SmartQuantDataProvider` 到物理表的路由发生在之后的 `bind_many()`，避免物理目录变化污染 LogicalPlan。

## 8. 阶段六：解析并冻结任务输出 Domain

拿到全部 `InputSpec` 后，Compiler 调用：

```python
domain = self._resolve_domain(request.domain, input_specs.values())
```

该方法会通过 DataProvider 读取两类轻量坐标元数据：

```text
provider.calendar_dates(calendar)
provider.asset_codes(asset_type)
```

具体工作包括：

1. 检查 `target_asset` 已声明在 `asset_scope` 中；
2. 检查所有 Source 使用兼容日历；
3. 从 Provider 完整日历裁剪请求日期闭区间；
4. 检查所有 Source 的资产类型都已出现在 `asset_scope`；
5. 对每个资产类型解析 `"all"` 或显式代码子集；
6. 保留显式子集顺序，拒绝重复代码和未知代码；
7. 为每个冻结资产轴计算稳定 `axis_fingerprint`；
8. 根据 `target_freq + target_step_count` 生成输出 step 标签；
9. 构造 `ResolvedOutputDomain`。

编译期间会同时保存每类资产的：

```text
(ordered codes, axis_fingerprint)
```

之后所有该资产类型的 `SourceTerm` 都引用这条任务级冻结轴。即使两个数组恰好具有相同列数，只要代码、顺序或轴指纹不同，也不会被误认为可对齐。

`ResolvedOutputDomain` 描述最终输出；它不会覆盖 Source 的原生频率或 step 数。

## 9. 阶段七：SourceRefExpr lowering 为 SourceTerm

Compiler 按输出逐棵递归调用 `_lower()`。遇到 `SourceRefExpr` 时进入 `_lower_source()`：

```text
SourceRefExpr
  + describe_many() 返回的 InputSpec
  + 本任务冻结的资产 codes / axis_fingerprint
  -> TermDomain
  -> SourceTerm
```

### 9.1 TermDomain 保留 Source 原生 Domain

`TermDomain` 包含：

```text
asset_type
codes
frequency
step_count
calendar
axis_fingerprint
```

其中 `frequency` 和 `step_count` 直接来自 `InputSpec`，不是任务 target。Compiler 只验证该组合能否生成合法 step 坐标，不会在 Source lowering 中执行隐式频率转换。

例如任务输出为 `stk.1d`，但 Source 是 `stk.5min`，生成的仍是 `stk.5min` SourceTerm。公式必须通过显式 `resample()` 将它转换为日频，或者编译失败。

### 9.2 SourceTerm 是 DAG 的取数叶子

`SourceTerm` 在通用 `Term` 字段之外保存：

- `source_ref`：逻辑数据身份和语义参数；
- `input_spec`：Provider 给出的静态输入契约。

它不保存：

- 数据库或 parquet 地址；
- 表名和字段名；
- 日期分区；
- `load_group_key`；
- 实际 ndarray。

因此 SourceTerm 可以参与稳定 DAG 身份计算，又不会把某次部署的物理细节固化进计划。

## 10. 阶段八：递归 lowering、CSE 和共享 DAG

算子 lowering 会先递归降低所有输入，因此 SourceTerm 总在依赖它的 OperatorTerm 之前进入拓扑顺序：

```text
SourceTerm(close) ------+
                        +-> OperatorTerm(add) -> OperatorTerm(ts_mean) -> output A
SourceTerm(volume) -----+          |
                                   +-----------------------------> output B
```

每个 Term 都先计算稳定 `semantic_key`，再交给 `Compiler._intern()`：

```python
existing = self._by_semantic_key.get(semantic)
```

如果已经存在等价 Term，Compiler 直接复用原 `term_id`；否则创建新 Term 并追加到 `topological_order`。

SourceTerm 的语义身份综合：

```text
SourceRefExpr.logical_key
SourceRefExpr.semantic_params
InputSpec
TermDomain
```

因此：

- 两个公式使用不同局部名称但引用同一 Source，可以共享一个 SourceTerm；
- 同一 Source key 使用不同复权或 PIT 参数，不会错误合并；
- 相同数组 shape 但资产轴身份不同，不会错误合并；
- 物理表或加载组变化，不改变逻辑计划身份。

OperatorTerm 同样按输入 Term、规范化参数、输出值类型和输出 Domain 做 CSE，因此公共子表达式可以跨公式共享。

## 11. 阶段九：生成 LogicalPlan 和 CompiledJob

所有输出 lower 完成后，Compiler 先校验每个输出 Term 能否落到任务目标 Domain。只允许已确认的 NumPy singleton 广播，其他资产或频率变化必须由显式 operator 表达。

随后 Compiler 生成：

```python
LogicalPlan(
    terms=...,
    topological_order=...,
    outputs=...,
    reference_counts=...,
    job_lookback=...,
    semantic_id=...,
)
```

各字段含义：

- `terms`：`term_id -> LiteralTerm | SourceTerm | OperatorTerm`；
- `topological_order`：依赖先于使用者的稳定执行顺序；
- `outputs`：`formula_id -> output term_id`；
- `reference_counts`：执行期及时回收中间数组所需的引用次数；
- `job_lookback`：所有输出所需的最大历史读取窗口；
- `semantic_id`：整个逻辑计算计划的稳定身份。

`LogicalPlan.source_terms` 会按拓扑顺序筛出全部 `SourceTerm`，供后续 Runtime 一次性交给 `bind_many()`。

最终返回：

```python
CompiledJob(
    plan=LogicalPlan(...),
    domain=ResolvedOutputDomain(...),
)
```

至此 DAG 编译结束，仍未读取任何因子数组。

## 12. DAG 之后：物理取数对象何时出现

虽然下列流程不属于 DAG 编译，但它解释了 SourceTerm 为什么不需要物理读取字段：

```text
CompiledJob
  -> PhysicalPlanner.partitions()
  -> 每个分区生成 ReadDomain
  -> Runtime.execute_partition()
  -> provider.bind_many(plan.source_terms, read_domain)
  -> SourceBinding(term_id, SourceSpec, ReadDomain, load_group_key)
  -> 按 load_group_key 分组
  -> provider.load_many(bindings)
  -> term_id -> ndarray
```

### 12.1 ReadDomain

`ReadDomain` 描述某个物理分区：

- 包含 lookback 的 `dates`；
- 本分区负责输出的 `write_dates`；
- 资产 `codes`；
- step 坐标；
- 写回完整结果的 `output_slice`。

### 12.2 SourceSpec

`SourceSpec` 才包含物理来源信息：

- `source`；
- `table`；
- `field`；
- Reader 参数；
- Source 默认参数与公式语义参数的合并结果。

例如 `SmartQuantDataProvider.bind_many()` 通过 `catalog.bind()` 从任务 Catalog 组装表、字段和读取参数，不依赖 Store 或 Router。

### 12.3 SourceBinding

`SourceBinding` 将三类信息连接起来：

```text
term_id       -> 对应哪个 SourceTerm
source_spec   -> 从哪里、按什么物理参数读取
read_domain   -> 读取哪个分区和原生坐标
load_group_key -> 可以与哪些 Source 合并加载
```

物理来源或加载组改变只影响执行方式，不改变 `LogicalPlan.semantic_id`。项目已有测试保证不同 load group 配置不会改变逻辑计划身份。

## 13. DataProvider 五个方法在完整生命周期中的职责

| 方法 | 调用阶段 | 是否允许读取大数组 | 作用 |
| --- | --- | --- | --- |
| `calendar_dates(calendar)` | 编译 Domain 解析、物理分区 | 否 | 提供权威日期主轴 |
| `asset_codes(asset_type)` | 编译 Domain 解析 | 否 | 提供权威有序资产主轴 |
| `describe_many(source_refs)` | 编译 | 否 | 批量返回 Source 静态契约 |
| `bind_many(source_terms, read_domain)` | 每个运行分区 | 否 | 解析物理来源、原生读取坐标和加载组 |
| `load_many(bindings)` | 每个运行分区 | 是 | 真正读取并返回 `term_id -> ndarray` |

编译路径严格只使用前三类元数据方法：

```text
compile()
  -> describe_many()
  -> calendar_dates()
  -> asset_codes()
  -X-> bind_many()
  -X-> load_many()
```

这一约束使公式和 DAG 可以在不执行昂贵 I/O 的情况下完成静态检查、Domain 验证和计划审阅。

## 14. 背后的设计思路

### 14.1 逻辑数据身份与物理位置分离

公式只引用稳定逻辑键和语义参数；Provider 在运行分区中再决定 Store、数据库、表、字段或文件。这样既能保持公式可移植，也允许物理数据目录独立演进。

### 14.2 先描述、后加载

`describe_many()` 在编译阶段提供足够的静态信息，使类型、资产轴、频率和 shape 错误在读取大数组前失败。`load_many()` 只负责执行已经通过编译检查的物理绑定。

### 14.3 Source 保留原生 Domain

SourceTerm 不自动迎合任务 target。Provider 描述什么频率和 step 数，SourceTerm 就保留什么 Domain。显式 `resample`、`align_frequency`、资产选择或合法 singleton 广播负责后续变换。

### 14.4 轴身份比 shape 更重要

NumPy shape 相同不代表数据可对齐。Compiler 冻结有序代码轴并计算 `axis_fingerprint`，让 Domain 校验能识别资产类型、代码集合和顺序差异。

### 14.5 批量描述和共享 DAG

Compiler 先收集全部输出可达的 Source，再调用一次 `describe_many()`；等价 Source 和公共算子子树通过 `_intern()` 做 CSE。多公式因此共享取数叶子和中间计算，而不是分别编译、分别加载。

### 14.6 名称服务于可读性，语义服务于复用

`close`、`price` 或 `x` 等 binding 名只存在于公式作用域。符号绑定后，DAG 身份由 Source、参数、Domain 和依赖结构决定，从而允许不同公式写法复用同一计算。

### 14.7 物理优化不能改变逻辑语义

加载组、批量 SQL、parquet 列裁剪、缓存和分区策略都应位于 SourceBinding/Provider 层。这些优化可以改变 I/O 次数和性能，但不能改变 LogicalPlan 及其 `semantic_id`。

### 14.8 最小对象各自承担单一职责

当前架构没有用一个大型 Source 对象贯穿所有阶段，而是让：

- `SourceRefExpr` 表达用户意图；
- `InputSpec` 表达静态契约；
- `SourceTerm` 表达 DAG 叶子；
- `SourceSpec` 表达物理位置；
- `SourceBinding` 表达某分区的实际读取任务。

这些对象看似相近，但生命周期和稳定性要求不同。分开后，每层都可以保持不可变、简单且容易测试。

## 15. 常见误解

### 15.1 `get_hf()` 会立即读取高频数据

不会。文本中的 `get_hf()` 先成为 `HelperExpr`，随后变成 `SourceRefExpr`。真正读取发生在运行分区的 `load_many()`。

### 15.2 `describe_many()` 会解析物理表并加载样本推断 shape

设计上不应如此。它应从权威元数据目录得到 `InputSpec`，不能依靠大规模数据读取完成编译。

### 15.3 `SourceSpec` 是 SourceTerm 的一部分

不是。`SourceSpec` 在 `bind_many()` 阶段产生，不进入 LogicalPlan。否则表迁移或加载组变化会不必要地改变计划身份。

### 15.4 任务 target 会自动改变所有 Source 的频率

不会。任务 target 只定义最终输出 Domain。SourceTerm 保留原生 Domain，不合法的混频计算会在 Compiler 中失败。

### 15.5 `common_inputs` 中声明的所有 Source 都会被描述和加载

不会。只有被至少一个最终输出引用的表达式才会被 Compiler 遍历；未使用的公共 input 不进入 DAG。

### 15.6 `get_hf(..., resample=...)` 会让 Provider 加载粗频数据

不会。`get_hf` 只构造原始频率 Source，再包装公开 `resample` operator；Provider 只会收到
原始 Source，聚合发生在 Runtime。它只是少写一次显式 `resample(get_hf(...), ...)`。

## 16. 审阅取数链路时的检查清单

修改或新增取数能力时，应至少检查：

1. 用户 API 最终能否规范化为唯一 `SourceRefExpr`；
2. Source 的所有结果相关参数是否进入 `semantic_params`；
3. `describe_many()` 是否只读取元数据并精确返回所有 Source；
4. `InputSpec` 是否正确描述 `asset_type/frequency/step_count/value_kind/calendar`；
5. SourceTerm 是否保留原生 Domain，而非偷用 target Domain；
6. 资产轴是否来自任务冻结的 Provider master axis；
7. 物理表、字段和加载组是否留在 `bind_many()` 之后；
8. 等价 Source 是否能跨公式 CSE，不同语义参数是否保持分离；
9. 未使用 input 是否不会进入 Source 描述和 DAG；
10. 编译过程是否完全不调用 `load_many()`；
11. 新 helper 是否在进入 lowering 前完全展开；
12. 物理 I/O 优化是否保持 `LogicalPlan.semantic_id` 不变。
