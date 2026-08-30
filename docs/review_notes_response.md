# 主流程 Review 理解核对与答疑

- 核对日期：2026-08-04
- 原始笔记：[`review_notes.md`](review_notes.md)
- 代码基线：当前 `batch-engine` 分支

本文逐条核对 `review_notes.md` 中的理解。结论以当前实现为准，并参考 [`FACTOR_ENGINE_DESIGN.md`](../FACTOR_ENGINE_DESIGN.md)；“设计目标”与“当前已经实现”会明确区分。

## 总体结论

主调用链的理解基本正确：

```text
FormulaBatch.from_text / 结构化 AST
  -> bind：名称解析与表达式内联
  -> _expand_helpers：HelperExpr 改写为规范 AST
  -> describe_many：SourceRefExpr -> InputSpec
  -> _resolve_domain
  -> _lower：规范 AST -> 共享 Term DAG
  -> PhysicalPlanner：按日期分区并加入 job lookback
  -> bind_many：SourceTerm -> 当前分区 SourceBinding
  -> load_many：加载 T × N × S 数组
  -> Runtime：执行 DAG、产生 ResultChunk
```

需要重点修正的认识是：

1. `operator_names` 不负责真正识别或注册 operator，它主要参与保留名称检查；未知 operator 延迟到 Compiler 报错。
2. 普通同频路径和显式 `resample` 都保留局部子树的资产 Domain；跨资产仍需显式投影。
3. `TermDomain` 的直接目的不是为未来 per-Term read domain 铺路，而是当前就用于类型/坐标语义校验、对齐 lowering 和 CSE 身份。
4. `workspace.pop()` 只删除 Workspace 中的引用，不保证 NumPy buffer 已经实际释放。

## 1. `FormulaBatch.from_text()`、三引号和非字符串入口

原理解正确。Python 的 `"""..."""` 和 `'''...'''` 是多行字符串语法，与因子引擎本身无关；`from_text()` 只是字符串 Adapter。

核心模型并不强制使用字符串。调用方已经可以直接构造：

```python
FormulaBatch(
    common_inputs=FormulaProgram(bindings=(...)),
    formulas={"alpha": FormulaProgram(bindings=(...))},
)
```

表达式也可以用 `source()`、`get_lf()` 和 `operator()` 等 Python helper 构造。因此，如果日后不希望 `common_inputs` 使用字符串，不必改变 Compiler 契约，只需提供更方便的结构化 Builder/Adapter。

## 2. helper 参数、复权和停牌语义

“helper 应允许携带复权、停牌等语义参数”的方向正确，当前语法层已经支持字面量关键字参数，例如：

```python
close = get_lf("stk", "ClosePrice", adjust="forward")
fund = get_fund("stk", "NetProfit", quarters=4)
```

调用链为：

```text
Parser 将关键字参数保存到 HelperExpr.params
  -> _expand_helpers() 通过 **params 写入 SourceRefExpr.semantic_params
  -> FeatureStoreDataProvider.bind_many() 合并到 SourceSpec.params
  -> Router/Reader 解释这些参数
```

但“参数能传递”不等于“语义已经实现”。当前日频 `_daily_field()` 不读取 `adjust` 参数，复权仍需要显式读取 `adj_factor` 并通过公式计算，或者增加真正的复权 helper 展开规则。停牌相关 Source 已存在，但没有任务级自动停牌策略。

另有一个明确冲突：`get_fund(..., quarters=4)` 会让 `FeatureStoreDataProvider.describe_many()` 返回 `InputSpec(frequency="1d", step_count=4)`，而 `_lower_source()` 当前强制日频 `step_count == 1`，因此新 batch pipeline 的多季度基本面会编译失败。这需要统一“日内 step”和“基本面季度维”的模型，不能只视为 helper 参数问题。

## 3. `helper_names` 和 `operator_names`

原理解部分正确。

- `helper_names` 决定函数调用被 Parser 构造成 `HelperExpr` 还是 `OperatorExpr`。
- `helper_names | operator_names` 共同组成保留名称，禁止写成 binding 左值。
- `operator_names` 当前不负责验证调用是否真是已注册 operator；任何不属于 `helper_names` 的简单函数调用都会先成为 `OperatorExpr`，真正的未知 operator 错误由 `_lower_operator()` 抛出。
- 给 Parser 增加自定义 `helper_names` 也不代表 helper 已可用；`Compiler._expand_helpers()` 仍必须增加对应改写分支。

因此这两个参数目前更接近 Parser 分类和保留字配置，不是完整的扩展注册机制。

## 4. `SourceSpan`

原理解正确。`SourceSpan` 保存：

```text
source / formula_id
line
column
```

它不参与 AST 结构相等、semantic hash 或 CSE，只用于把 parse、symbol binding 和 compile 错误定位回用户公式。

## 5. `ExecutionOptions` 是否缺少 task-level mask

当前确实没有任务级 `input_mask/output_mask`，但它们不应加入 `ExecutionOptions`。

设计约束是：不同 `ExecutionOptions` 只能改变执行方式，不能改变最终数值；`chunk_size` 属于执行策略，而 mask 会改变公式语义。

当前可以显式写成公式：

```python
masked_close = apply_mask(close, input_mask)
raw_factor = some_operator(masked_close)
factor = apply_mask(raw_factor, output_mask)
```

如果需要任务级统一策略，更合理的位置是：

- `ComputeRequest` 中新增明确的语义字段；或
- `FormulaBatch` 增加公共 mask 定义；或
- Compiler 在所有 Source/Output 边界插入结构化 mask Term。

在增加接口前必须先明确：input mask 是应用到每个原始 Source、每个公式入口，还是特定 operator 的样本集合；output mask 的 Missing 应如何处理。否则一个简单布尔字段会隐藏不一致的计算语义。

## 6. `load_factor("alpha_1")` 与 `factor:` 约定

原理解正确。

```text
load_factor("alpha_1")
  -> SourceRefExpr("factor:alpha_1")
  -> RepositoryDataProvider.describe_many() 读取已保存 metadata
  -> RepositoryDataProvider.bind_many() 生成临时仓库 SourceBinding
  -> repository.load() 读取数组
```

它引用的是此前已经完整提交到外部仓库的因子，不是当前 FormulaBatch 中另一个公式的局部结果。当前 `factor:` 前缀和临时目录格式都只是验证闭环的临时契约，尚不是正式 FactorRepository 设计。

## 7. `project_stk_to_cb` 与行业统计

### `project_stk_to_cb`

当前 helper 展开为：

```text
project_stk_to_cb(values, mapping)
  -> lookup_by_col(values, mapping)
```

Compiler 允许左侧是股票 Domain、mapping 是可转债 Domain，输出 Domain 跟随 mapping。这条路径已经支持“先在股票轴上计算局部特征，再投影到可转债”。

但当前实现确有明显限制：`lookup_by_col()` 把 mapping 数值直接解释成 `source_values` 的列位置，而不是股票 InnerCode。`CBStockMap` Reader 生成列位置时使用的是 Store 的完整股票 master axis；如果请求通过 `asset_scope` 选择股票子集或重排代码，`source_values` 的列轴已经改变，旧列号就可能错误或越界。

因此当前实现只在“股票 SourceTerm 轴与 Store master axis 完全同序”时可靠。可选修复方向：

1. `bind_many/load_many` 按当前 `term.domain.codes` 重新生成映射列号；或
2. mapping 保留稳定 InnerCode，由显式 projection operator 根据源 Term 的 codes 建立位置映射；或
3. 将资产投影作为独立 Compiler/Provider 协议，而不是让普通数值数组携带脆弱列号。

第二种语义最稳定。

### 行业统计

当前已有通用 operator：

```text
group_mean/group_sum/group_std/group_demean/group_zscore
```

它们接受 `NUMERIC + CODE`，因此可以通过行业代码 Source 实现行业统计；项目没有 `industry_mean()` 一类行业专用 helper。如果希望统一行业标准、层级和样本 mask，可以增加 helper，把业务便利写法展开成行业 SourceRef + 通用 group operator。

还要注意，`FeatureStoreDataProvider` 默认把未配置的 Source 描述为 `NUMERIC`。行业代码必须通过 `source_kinds` 显式声明为 `ValueKind.CODE`，否则 `group_mean` 等 operator 会在编译期类型校验失败。当前 Source 配置中的字段名称本身不会自动推导 `ValueKind.CODE`。

## 8. helper 和 formula 的完整生命周期

```text
用户字符串
  -> FormulaParser._convert()
     helper 调用 -> HelperExpr
     operator 调用 -> OperatorExpr
     程序名称 -> SymbolRefExpr
  -> FormulaBatch.bind()
     SymbolRefExpr 被绑定表达式递归替换
     每个公式只保留最后一个 binding 的完整输出表达式
  -> Compiler._expand_helpers()
     source/get_lf/get_hf/get_fund/load_factor -> SourceRefExpr
     resample/project/index broadcast -> 规范 OperatorExpr
  -> Compiler._lower()
     LiteralExpr -> LiteralTerm
     SourceRefExpr -> SourceTerm
     OperatorExpr -> OperatorTerm
  -> _intern()
     按 semantic key 跨公式 CSE
  -> Runtime
     只看 Term DAG，不再知道 helper、局部名称或 FormulaProgram
```

Helper 是编译期语法糖，不是 Runtime 函数。

## 9. Source 的完整生命周期

```text
HelperExpr，例如 get_lf(...)
  -> SourceRefExpr
     稳定逻辑 key + semantic params
  -> DataProvider.describe_many()
  -> InputSpec
     asset/frequency/step_count/value_kind/calendar
  -> Compiler._lower_source()
  -> SourceTerm
     SourceRef + InputSpec + TermDomain + semantic identity
  -> PhysicalPlanner
  -> 当前分区 ReadDomain
  -> DataProvider.bind_many()
  -> SourceBinding
     term_id + SourceSpec + term-specific ReadDomain + load_group_key
  -> Runtime 按 load group 调用 DataProvider.load_many()
  -> term_id -> float64 T × N × S
  -> Workspace
```

其中 `InputSpec` 是编译语义，`SourceSpec` 是物理位置；改变数据库表而不改变数据产品语义时，不应改变 LogicalPlan 身份。

## 10. DataProvider、日历和 lookback

“正式 DataProvider 尚未完成”作为产品判断基本合理，但需要区分接口与后端：

- `DataProvider` Protocol、Memory/FeatureStore/Repository 三个 Adapter 已实现。
- `FeatureStoreDataProvider` 是可运行的真实数据适配器，但复用了既有 Store、Router 和 SmartQuant Reader，并不是最终独立的数据平台。
- 正式 Source Catalog、批量查询、版本/快照治理仍不完整。

当前日期和 lookback 流程是：

```text
Compiler._resolve_domain()
  provider.calendar_dates(calendar)
  -> 在完整 Snapshot 日历上截取 start/end，得到输出 dates

PhysicalPlanner.partitions()
  再取完整 calendar
  -> 对每个 write_dates 向前扩展 job_lookback 个交易 session
  -> 生成 read_dates

bind_many/load_many
  -> 按 read_dates 读取

Runtime
  -> 计算完整 read_dates
  -> 只切出 write_dates 产生结果
```

当前 lookback 是全任务最大值，不是 per-Term。日历历史不足时读取可用前缀，rolling 的 `min_periods` 决定前部输出是否为 NaN。

## 11. 资产轴来自哪里

笔记中的 `provider._asset_axis` 应改为 `Compiler._asset_axes`。

`Compiler._resolve_domain()` 对 `asset_scope` 中每种资产调用：

```python
provider.asset_codes(asset)
```

- `FeatureStoreDataProvider` 从 Store 固定 master axis 读取；
- `MemoryDataProvider` 从初始化时传入的 codes 读取；
- `RepositoryDataProvider` 委托基础 Provider。

Compiler 再应用 `"all"` 或显式子集，并保存为任务级 `_asset_axes`。因此“当前真实 Provider 的资产轴复用 Store codes”是正确的，但不是 DataProvider 协议本身的限制。

## 12. `_lower_source()` 是否重复复制 codes，以及校验是否必要

当前确实存在多次表示转换：

```text
provider ndarray
  -> selected ndarray
  -> codes_tuple
  -> output ndarray
  -> TermDomain tuple
```

但 `_lower_source()` 为多个 Term 创建 `TermDomain` 时，`codes` 通常引用 `_asset_axes` 中同一个不可变 tuple，并不会为每个 SourceTerm 深拷贝全部 codes。主要额外成本是多个小 dataclass 和 hash 输入，而不是反复复制数据数组。

`DomainSpec` 统一的是任务允许的日期/资产范围和目标输出，不代表所有 SourceTerm 已天然处于同一 Domain。内部仍需校验：

- Provider 声明的 asset 是否在 `asset_scope`；
- Source 的 frequency/step 契约是否自洽；
- 两个 operator 输入是否同轴；
- 何处需要 step broadcast/resample；
- 最终输出是否精确等于 target Domain；
- 相同 shape 但不同资产身份不能误算。

可以简化的是表示和重复推导，例如当前 `frequency` 与 `step_count` 在普通行情场景互相可推导，`codes` 与 `axis_fingerprint` 也同时保存了轴身份。但不能仅依靠一次 DomainSpec 校验后完全取消 Term 级检查。

需要特别处理前述多季度基本面：当前“日频必须单 step”的检查过强，与 Provider 的 quarters 模型冲突。

## 13. 跨资产局部计算和对齐策略

“同一个 operator 内输入需要兼容，不同局部子树可以处于不同资产 Domain”是正确方向。普通 operator 和 `lookup_by_col` 路径已经按此实现，但当前 Domain lowering 尚不完全一致。

例如目标是可转债时可以：

```text
stk close
  -> stk ts_mean
  -> stk rank
  -> project_stk_to_cb(mapping)
  -> cb result
```

在上述同频例子中，股票子树不需要提前转为 `target_asset=cb`。`_lower_source()` 在做日频到日内广播或粗频到细频投影时也保留输入的 `asset_type/codes`，只改变 frequency/steps。普通 operator 收集其直接输入的非标量 Domain，只有出现多个不兼容 Domain 才失败；`lookup_by_col` 是显式跨资产例外。

`_lower_resample()` 只替换输入 Domain 的 frequency 和 step_count，保留 asset、codes、
calendar 和 axis identity。例如目标为 `cb.1d`、输入为 `stk.5min` 时：

```text
resample(stk.5min -> 1d)
```

重采样结果仍是 `stk.1d`，之后必须由 `project_stk_to_cb` 显式转换资产轴。目标频率也不与
任务 `target_freq` 耦合，因此局部子树可以先聚合到中间频率，再继续参与后续显式计算。

不建议“不同情况下优先自动往 target_asset 转”，因为转换通常需要 mapping、selector 或 reducer：

- `stk -> cb` 需要每只转债唯一正股映射；
- `cb -> stk` 通常是多对一，需要聚合规则；
- `idx -> stk` 需要先选定具体指数。

无法唯一推导时，显式 projection 比隐式 target 优先规则更安全。可以改进的是把 projection 规则集中成清晰的 Domain-lowering policy，而不是去掉显式语义。

## 14. `TermDomain` 是否过重

当前 `TermDomain` 包含：

```text
asset_type
codes
frequency
step_count
calendar
axis_fingerprint
```

它不包含 dates；当前分区日期只存在于 `ReadDomain`。

只保存 `asset + freq` 在当前单任务、单轴实现中看似足够，但一旦允许显式资产子集或调用方自定义顺序，同一个 `stk.1d` 可以有不同 codes/order。两个数组即使 shape 相同，也不能在代码顺序不同的情况下直接相加，因此至少需要稳定轴身份。

可以讨论的简化是：

- TermDomain 只保存 `axis_id/fingerprint`，codes 统一存放在任务级 axis catalog；
- step identity 使用独立 `StepSpec`，避免 `frequency + step_count` 重复；
- calendar 在当前单日历批次中保存一次引用。

但不能把它缩减到只有 asset/freq；那会丢失显式子集、代码顺序和坐标身份。它的主要作用是当前语义正确性，不是精细 lookback。

## 15. TermDomain、频率规则和未来 per-Term ReadDomain

“TermDomain 能为未来 per-Term read domain 提供基础”可以作为附带演进可能，但不是当前设置它的主要原因。

当前它已经用于：

- operator 输入兼容性；
- 日频到日内的 broadcast lowering；
- 粗频到细频 ffill；
- 显式 resample；
- 股票到转债、指数到目标轴投影；
- 输出 Domain 校验；
- Term semantic identity 和 CSE。

per-Term lookback/offset 当前明确未实现；所有 SourceTerm 使用 job 级最大 ReadDomain。未来若实现 per-Term 读取，还需要在依赖图上反向传播日期需求，只有 TermDomain 本身并不够。

频率对齐规则目前确实分散在：

```text
domain.py：step 轴和频率映射算法
compiler.py：何时插入 Domain operator
operators：具体数组变换
execution.py：内部 Domain operator 的 Runtime 注册
```

这属于“策略、规划、执行”分层，但当前内部 operator 注册和策略入口可以进一步集中，降低理解成本。

## 16. `PhysicalPlanner` 的分块方式

原理解正确。当前只在输出日期轴上切片：

```text
chunk_size -> write_dates
write_dates 向前扩展 job_lookback -> read_dates
```

它不按资产或 step 分块，也不做内存预算、自适应分区或 per-Term offset。

## 17. Domain 类和 `ExecutionScope` 是否多余

这些类存在真实语义区别：

| 类型 | 作用 |
|---|---|
| `DomainSpec` | 用户请求的范围与目标 |
| `ResolvedOutputDomain` | 已解析的精确输出坐标 |
| `TermDomain` | 每个 Term 的静态资产/频率/轴身份 |
| `ReadDomain` | 当前物理分区实际读取的 dates/codes/steps |
| `ExecutionScope` | Store/Router 使用的 read/write dates 适配对象 |
| `PhysicalPartition` | partition id、output slice 和 ReadDomain |

`ReadDomain` 必须按 SourceTerm 重建，因为一个任务可能同时读取股票、转债和指数，也可能同时读取日频和日内数据。

不过 `ExecutionScope` 与 `ReadDomain` 确有重叠。当前它主要是新 DataProvider 向既有 Store/Router 传递日期范围的桥接层，只携带 dates，不携带完整 codes/steps。后续如果 Store 完全迁移到新 Provider 契约，可以重新评估是否合并或明确为 Store 专用类型。

## 18. 是否实现了真正的 `load_many()`

需要区分 API 批量和物理 I/O 批量。

当前已经实现：

- Runtime 按 `load_group_key` 分组；
- 每组只调用一次 Provider `load_many()`；
- 同组失败整体 fail-fast；
- 测试验证同表 close/volume 只调用一次 Router `load_many()`。

当前尚未实现：

- `DataRouter.load_many()` 内部仍然 `for spec in specs: read_spec(...)`；
- Store 无 Router 路径也是逐 feature 调用 `load_feature()`；
- SmartQuant Reader 没有同表多字段一次 SQL 的通用实现。

所以接口和调度层是真正的 batch contract，当前默认后端仍是逐字段 fallback。设计文档明确允许第一版如此，但性能收益尚未实现。

## 19. `args = [workspace[input_id] ...]` 是否复制数组

不会复制 ndarray 数据。

这条列表推导只创建一个新的 Python list，list 中保存对 Workspace 数组对象的引用：

```text
workspace[id] ─┐
               ├── 指向同一个 ndarray
args[i] ───────┘
```

真正是否复制取决于 operator：

- `np.broadcast_to` 通常产生 view；
- 普通加减乘除通常产生新数组；
- 高级索引通常复制；
- 某些 `astype(..., copy=True)` 明确复制。

因此读取 `args` 本身开销很小，但 operator 实现仍需单独审查内存行为。

## 20. `workspace.pop()` 是否正确释放内存

它能正确实现“从逻辑 Workspace 删除不再需要的 Term”，但不能保证底层数组已经实际释放。

在 CPython 中，`pop()` 会减少一个引用；只有不存在其他引用时，ndarray 对象及其 buffer 才可能立即释放。当前仍可能有其他引用：

1. 局部变量 `args` 会继续引用刚刚使用的输入，直到被重新赋值或函数返回；
2. 局部变量 `loaded` 会保留最后一次 load group 返回的全部数组，当前没有显式 `del loaded`；
3. `value` 可能继续引用最近的 operator 输出；
4. NumPy view 会通过 `.base` 保留底层数组；
5. `DataRouter._cache` 会缓存按 scope 加载的 `FeatureArray`，可能跨分区保留 Source 数组。

因此 `ExecutionStats.peak_workspace_values` 统计的是 Workspace entry 数，不是实际峰值内存，也忽略 view base、Provider cache 和局部引用。

若要让实际内存行为更接近逻辑生命周期，可考虑：

- operator 调用和释放后显式清理 `args`；
- Source group 写入 Workspace 后清理 `loaded`；
- 明确 Router cache 的任务/分区生命周期并及时 `clear_cache()`；
- 记录 ndarray `nbytes`、共享 buffer 和 view，而不是只统计 entry 数；
- 通过 profiling 验证，而不是仅依赖引用计数推断。

## 21. “总体上校验过多”如何判断

不能只按校验数量判断，建议按边界和失败成本分类。

当前合理且必要的校验包括：

- Parser 只接受受限公式语法；
- bind 检查作用域和非法名称；
- Provider 返回项必须覆盖全部 SourceTerm；
- 外部数组必须是精确 `float64 T × N × S`；
- operator 输入 ValueKind 和 Domain 必须兼容；
- 最终输出必须匹配 target Domain；
- ResultStream 必须自然结束才算成功。

这些校验位于不可信边界，能够避免“shape 恰好相同但语义错误”的静默错算。量化引擎中，静默错算通常比显式失败风险更高。

可以进一步审查或简化的部分包括：

- 字符串 Parser 与 bind 对重复名称/保留名称存在防御性重复，后者主要服务直接 AST 入口；
- frequency 与 step_count 的重复表达及多季度基本面冲突；
- codes 与 axis_fingerprint 同时嵌入每个 TermDomain；
- Provider 与 Runtime 对外部数组契约的责任边界可以更明确；
- `bind_many()` 使用 set 比较，实际不能检测同一 `term_id` 被重复返回，和“恰好一个 binding”的错误文案不完全一致；
- `_validate_operator_result()` 会把结果强制转换为 float64，这更像规范化而不是严格验证。

建议保留边界验证，合并重复表示，并修正验证规则与真实模型不一致的地方。

## 建议优先确认的实现问题

按对正确性和后续设计的影响排序：

1. **统一 step 语义**：解决 `1d + quarters>1` 与 `_lower_source()` 固定频率 step 数检查的冲突。
2. **修正跨资产映射身份**：`project_stk_to_cb` 不应依赖 Store master axis 的脆弱列号，尤其要覆盖显式子集和代码重排。
3. **确定任务级 mask 契约**：放入语义请求/Compiler，而不是 `ExecutionOptions`，并先定义应用位置与 Missing 规则。
4. **明确 DataProvider 的完成边界**：当前 batch API 已有，真正同表多字段 I/O 尚未实现。
5. **用 profiling 验证内存释放**：处理 Runtime 局部引用和 Router cache，避免把 `workspace.pop()` 等同于实际内存回收。
6. **集中 Domain alignment policy**：保留显式对齐语义，同时减少规则在 Compiler、domain helper 和 Runtime 注册之间的分散。

除上述问题外，当前主架构的分层是自洽的：公式语义、编译期 Source 描述、物理 Source binding、数组加载和 Runtime 执行已经被明确分开。
