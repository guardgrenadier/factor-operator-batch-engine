# core 当前问题与近期优化建议

> 状态：问题诊断与实施建议  
> 日期：2026-07-29  
> 范围：当前 `core/` 实现  
> 非目标：本文不以 `core/docs/refactor/` 中的远期架构为依据，也不要求一次性重写现有系统

## 1. 文档目的

本文记录当前代码走查中发现的设计、正确性、可维护性和性能问题，并给出可以逐步落地的修复顺序。

近期优化的主线应当是：

```text
先明确 definition / alias / source / materialized feature 的身份与复用契约
  -> 再把 Planner 变成可检查、可验证的计划
  -> 再建立明确的数组生命周期
  -> 最后做 CSE、buffer reuse 和算子融合
```

不建议在当前表达式树和执行模型上直接增加复杂融合或并行执行。否则会放大现有的取数歧义、内存占用和可审阅性问题。

## 2. 总体结论

当前最关键的问题并不是某个算子实现得不够快，而是需要把完整 key 的身份和复用契约表达得更明确。当前同一个完整 key 会沿以下层级解析：

1. 已注册的 `FeatureDef`。
2. Store 中已经物化的 feature。
3. DataRouter 可以读取的外部 source。
4. Calculator 当前会话中的 runtime feature。

对于 source 字段，Store 优先于 DataRouter 是当前设计的一部分：在同一个 Snapshot 中，只要 source key 和读取参数没有变化，就复用已经物化的结果；只有参数变化并形成不同的 source identity 时，才重新拉取。

当前 `get_fund()` 已经通过参数化 raw source name 实现了这项契约。后续工作主要是把它记录为稳定行为、补足回归测试，并要求未来的动态 source 沿用同一规则：

- 相同 source key 必须表示相同的 source 类型和读取参数。
- 影响取数结果的参数必须进入自动生成的 source name。
- `name` 只控制输出 feature 的名字，不改变底层参数化 raw source identity。
- Planner/debug 输出应说明当前叶子是复用 Store 结果，还是重新读取外部 source。

建议按以下优先级处理：

| 优先级 | 问题 | 主要影响 |
| --- | --- | --- |
| P0 | alias 存在多个事实来源 | 覆盖、rename、加载后可能产生陈旧绑定 |
| P1 | helper 自动生成的 key 缺少可发现、可组合的引用方式 | 用户需要手工重写 key，容易在后续定义中拼错 |
| P1 | Fundamental 依赖 Manager bridge | definition 与取数边界耦合 |
| P1 | Planner 输出仍是普通 Expr 树 | 难以 review、验证和优化 |
| P1 | Router/Calculator 强引用完整数组 | 高频场景峰值内存不可控 |
| P2 | 参数化 source naming 契约缺少统一测试和扩展协议 | 新动态 source 可能遗漏参数命名 |
| P2 | Executor 没有 DAG 和节点生命周期 | 公共子表达式重复计算，不能及时释放 |
| P2 | OperatorSpec 未承载完整算子语义 | Planner 中存在大量按算子名硬编码 |
| P3 | 缺少批量取数、CSE 和 buffer reuse | I/O、计算与内存均有冗余 |
| P3 | Planner 和 chunk 测试覆盖不足 | 后续改造缺少可靠回归基线 |

## 3. 已确认问题

### FIX-001：固化参数化 source naming 与物化复用契约

#### 当前行为

`Executor._load_feature_like()` 按以下顺序读取叶子：

```text
runtime_features
  -> FeatureStore
  -> DataRouter
```

相关代码：

- `core/engine.py::_load_feature_like`
- `core/manager.py::get_fund`
- `core/sources.py::fundamental_name`

`get_fund()` 会先调用：

```python
raw_name = fundamental_name(
    field,
    column_name,
    quarters,
    data_code,
    publ_date_limit,
)
```

然后生成：

```python
raw_key = f"{asset}.{freq}.{raw_name}"
out_name = name or raw_name
```

因此当前语义是：

1. 不指定 `name` 时，输出 feature name 与参数化 raw source name 相同。
2. 指定 `name` 时，只改变输出 feature name。
3. 无论是否指定 `name`，底层 dependency 都使用自动生成的 `raw_key`。
4. `column_name`、`quarters`、显式 `data_code` 和非默认 `publ_date_limit` 会编码进 raw source name。
5. 参数变化会得到不同 raw source key，从而重新拉取；参数相同则可以复用 Store 中已经物化的 source。

例如：

```text
get_fund(field="Revenue", column_name="value", quarters=1)
  -> raw source: stk.1d.Revenue_value
  -> output:     stk.1d.Revenue_value

get_fund(field="Revenue", column_name="value", quarters=4)
  -> raw source: stk.1d.Revenue_value_4Q
  -> output:     stk.1d.Revenue_value_4Q

get_fund(field="Revenue", column_name="value", quarters=4, name="revenue")
  -> raw source: stk.1d.Revenue_value_4Q
  -> output:     stk.1d.revenue
```

#### 设计解释

这套行为符合当前 Snapshot 设计：

- source name 是字段和读取参数共同决定的逻辑身份。
- 相同 raw source key 代表同一份可复用输入。
- Store 优先于 DataRouter，避免相同参数重复拉取。
- `overwrite=True` 只表示允许覆盖目标 feature，不表示强制刷新其 source 依赖。
- 外部底层数据在 source identity 不变时发生变化，不自动穿透 Snapshot 的可复现边界。

因此 FIX-001 不是要改变当前读取顺序，而是把现有正确行为变成明确、受测试保护的 source naming 协议。

#### 需要补强的边界

1. 为 `fundamental_name()` 建立参数组合测试。
2. 明确默认参数不写入 name、非默认参数写入 name 的规则。
3. 明确 `name` 是 output name，不是 raw source name override。
4. 新增其他动态 source 时，要求提供对应的 canonical source naming 函数。
5. source naming 函数应避免字段名和参数字符串拼接产生歧义；必要时使用结构化编码或短 hash。
6. debug plan 应同时显示 output key 和 raw source key，减少用户误解。
7. 如果未来需要强制刷新相同 identity 的 source，应增加独立的 Snapshot refresh 流程，而不是改变 `overwrite` 的含义。

显式输入绑定仍然有价值，但其目的是让 Planner 和内存生命周期可见，而不是改变 Store-first 复用语义：

```text
MaterializedSourceInput(raw_source_key, ...)
ExternalSourceInput(raw_source_key, source_spec, ...)
RuntimeInput(key, ...)
```

#### 验收标准

- 不指定 `name` 时，output name 等于自动生成的 raw source name。
- 指定 `name` 时，只改变 output name，raw source dependency 保持参数化命名。
- 相同字段和参数稳定生成相同 raw source key。
- 任一有效取数参数变化时生成不同 raw source key，并触发重新拉取。
- 相同 raw source key 稳定复用 Store，不重复拉取。
- `overwrite=True` 不隐式刷新 source。
- debug 输出能够同时显示 output key、raw source key 和实际输入 origin。

### FIX-002：definition 与 alias 存在多个事实来源

#### 当前状态

alias 同时存在于：

- `FeatureDef.alias`
- `FeatureManager.aliases`
- `feature_defs/aliases.json`
- 已物化 feature metadata 中的 `FeatureDef.alias`
- `Calculator.aliases`

其中：

- `FeatureManager.aliases` 是 `alias -> key`。
- `Calculator.aliases` 是 `alias -> Expr`。
- 注册定义时 alias 会被冻结成完整 key。
- Calculator 又保留了一次运行时 alias 解析能力。

这使得“alias 是定义属性、独立命名索引，还是公式运行时符号”没有唯一答案。

#### 已确认异常

当同一个 key 原有 alias 为 `old`，随后以 `overwrite=True` 注册 alias 为 `new` 的新定义时，结果会同时保留：

```python
{
    "old": "stk.1d.a",
    "new": "stk.1d.a",
}
```

此外还有以下边界问题：

- `add_alias()` 和 `replace_alias()` 不校验目标定义是否存在。
- `rename()` 只移除 `FeatureDef.alias`，不会系统更新所有指向旧 key 的动态 alias。
- `rename(alias=None)` 无法表达“清除已有 alias”。
- 已物化 key 的 rename 不会同步迁移 Store 目录和 manifest。

#### 修复建议

建立唯一的 `AliasRegistry`：

```text
AliasRegistry
  alias -> canonical feature key
```

职责约束：

1. `FeatureDef` 只保存 canonical key 和计算语义。
2. alias 只用于用户输入和公式注册时的符号解析。
3. 定义注册完成后，公式、mask 和 delay_dict 中只保存完整 canonical key。
4. Planner 对已注册 FeatureDef 不再重复解析 alias。
5. `FeatureDef.alias` 先标记 deprecated；加载旧数据时迁移进 AliasRegistry。
6. `aliases.json` 是 alias 的唯一持久化文件。

对于 rename：

- 未物化定义允许修改 key，但必须事务性更新 definition index 和所有 alias。
- 已物化 feature 不允许静默 rename，应提供显式 store migration/copy 操作。

#### 验收标准

- 覆盖定义不会遗留旧 alias。
- alias 的保存、加载、覆盖和删除只有一份事实来源。
- 注册后的 FeatureDef 序列化内容中不含 `AliasExpr`。
- alias 指向不存在 key 时立即报错。

### FIX-003：FeatureDef 混合了定义语义和执行策略

`FeatureDef` 当前同时包含：

- 因子身份：`key/asset/freq/name`
- 计算语义：`formula/mask/delay/steps`
- 用户命名：`alias`
- 执行策略：`materialize/overwrite`
- helper 和取数参数：`params/metadata`

其中 `materialize`、`overwrite`、`chunk_size`、`workers` 属于一次执行请求，而不是稳定的因子定义。把它们保存在 FeatureDef 中会造成：

- 同一公式因为执行策略不同而产生多份定义。
- Store metadata 中混入本次运行策略。
- Manager 需要同时解释定义语义和调度策略。

另外，`FeatureDef` 重复保存 `key/asset/freq/name`。`from_key()` 会保持一致，但直接构造和 `from_dict()` 没有统一校验。

#### 修复建议

新增执行请求对象：

```python
MaterializationRequest(
    key=...,
    overwrite=False,
    chunk_size=None,
    workers=1,
    memory_budget_mb=None,
    return_array=True,
)
```

FeatureDef 只保留影响计算结果和 provenance 的字段。

近期至少应增加 `FeatureDef.__post_init__`：

1. 使用 `parse_feature_key(key)`。
2. 校验 `asset/freq/name` 与 key 一致。
3. 校验 `steps > 0`。
4. 对 params、metadata 和 delay_dict 做防御性复制或冻结。

中期可以只保存 `FeatureKey`，把 `asset/freq/name` 改为派生属性。

### FIX-004：Fundamental bridge 破坏 definition 与 source 边界

#### 当前行为

`get_fund()` 把 fundamental 参数放入 FeatureDef：

```text
params
metadata["helper"] = "get_fund"
dependencies = (raw_key,)
```

materialize 时，`FeatureManager._register_fundamental_source()` 再检查 helper metadata，从定义中反向重建 `SourceSpec`，注册到 DataRouter override。

这意味着：

- Manager 需要知道 Fundamental source 的业务参数。
- metadata 实际参与了核心执行语义。
- 新增类似的动态 source 时，Manager 还会继续增加专用 bridge。
- 直接引用 fundamental raw key 时无法获得同等行为。

#### 修复建议

推荐让 `get_fund()` 直接构造可序列化的 SourceRef：

```text
SourceRef(
    key=raw_key,
    spec=SourceSpec(
        source="Fundamental",
        field=...,
        params=...,
    ),
)
```

由通用 BindInputs pass 把 SourceRef 转为 `ExternalSourceInput`。Manager 不再识别 `metadata["helper"]`，也不再包含 Fundamental 专用代码。

如果近期不新增 SourceRef，可以先在 FeatureDef 中增加通用：

```python
source_inputs: dict[str, SourceSpec]
```

Manager 只负责统一注册 `source_inputs`，不判断 source 类型。这个过渡方案仍比 Fundamental 专用 bridge 更清晰。

#### 验收标准

- 删除 `_register_fundamental_source()`。
- `get_fund()` 的所有取数参数都有唯一、显式、可序列化的载体。
- Fundamental、Signal 和未来动态 source 使用同一绑定机制。

### FIX-005：DataRouter 初始化和缓存生命周期过重

#### 当前行为

`DataRouter.__init__()` 会立即调用 `build_data_dict()`，扫描：

- source table schema
- minute parquet schema
- Fundamental item dictionary

因此即使用户只使用 memory source 或精确配置 source，构造 Router 仍可能访问数据库和文件系统。

Router 还持有一个无大小限制的强引用缓存：

```python
self._cache: dict[str, FeatureArray]
```

Manager 主路径会在 finally 中清理，但低层 Calculator 或 Router 直接使用时，数组可能一直存活到 Router 销毁。

#### 修复建议

1. Router 初始化只加载静态配置，不扫描外部系统。
2. `search()` 首次调用时 lazy build data dictionary。
3. 提供显式 `refresh_catalog()`。
4. source array cache 移到单次 `ExecutionSession`。
5. cache 支持：
   - 最大字节数；
   - LRU 或按 consumer count 释放；
   - `close()/clear()`；
   - 命中、未命中和实际读取字节统计。
6. `register_source()` 遇到同 key、不同 SourceSpec 时应报冲突，而不是静默覆盖。

### FIX-006：Planner 难以 review 和验证

#### 当前状态

Planner 目前依次做 alias 和若干 rewrite，但最终仍返回普通 Expr。表达式节点没有：

- node id
- 已绑定输入来源
- asset/freq/steps 推断结果
- dtype 和 missing-value 语义
- consumer count
- 预计输出大小
- pass diagnostics

Planner 中还存在按算子名硬编码的规则：

- `SAMPLE_AWARE_OPS`
- `DAILY_FROM_INTRADAY_OPS`
- `infer_date_overlap()` 中的算子集合

另一方面，`OperatorSpec.output_asset/output_freq/output_step/preserves_shape` 已经存在，却没有成为统一的 Planner 推断协议。

#### 修复建议

将 Planner 渐进拆成显式 pass：

```text
Parse
  -> ResolveSymbols
  -> BindInputs
  -> InferValueSpec
  -> AlignAxes
  -> ApplyTemporalPolicy
  -> InjectMasks
  -> CanonicalizeAndCSE
  -> LowerToDAG
  -> Validate
```

每个 pass：

1. 输入和输出类型明确。
2. 不执行 I/O。
3. 不修改共享全局状态。
4. 可以单独运行单元测试。
5. 可以输出 before/after debug 信息。

建议增加：

```python
ValueSpec(
    asset=...,
    freq=...,
    steps=...,
    dtype=...,
    missing_value=...,
)

PlanResult(
    root=...,
    nodes=...,
    inputs=...,
    diagnostics=...,
    overlap=...,
    estimated_peak_bytes=...,
)
```

短期可以继续复用现有 Expr，只需先让 `Planner.plan()` 返回 `PlanResult(expr=..., trace=...)`。待行为稳定后再 lower 成真正的 DAG。

#### OperatorSpec 应补充的协议

- 输入数量和参数 schema
- shape/type inference
- 算子类别：elementwise、rolling、reduction、mapping、resample
- sample mask 参数位置
- overlap 推断函数
- 是否 pure
- 是否支持 `out=`
- 是否允许 fusion

这样新增算子时不需要同时修改多个硬编码集合。

### FIX-007：当前内存生命周期不适合高频计算

#### 当前持有关系

一次全量 materialize 中，数组可能同时被以下对象持有：

- `DataRouter._cache`
- `Calculator.runtime_features`
- `Executor._leaf_cache`
- 当前递归求值栈
- NumPy 算子生成的临时结果
- 最终 `FeatureArray`

`Calculator.runtime_features` 会保留本次会话计算过的所有命名依赖，即使其最后一个消费者已经执行完成。Executor 只缓存叶子，不缓存或管理普通表达式节点。

#### 高频规模估算

假设：

```text
T = 252 个交易日
N = 6000 个标的
S = 237 个分钟 step
dtype = float64
```

单个完整数组约为：

```text
252 * 6000 * 237 * 8 bytes ≈ 2.67 GiB
```

即使 20 天一个 chunk，单数组仍约为 217 MiB。几个输入、mask 和中间结果同时存活时，峰值内存会迅速上升。

#### 修复建议

引入 `ExecutionSession`：

```text
ExecutionSession
  - bound input cache
  - runtime values
  - pinned outputs
  - node consumer counts
  - live bytes / peak bytes
  - memory budget
```

在 DAG 上预先计算每个节点的 consumer count：

1. 节点执行后保存结果。
2. 每消费一次，对输入 consumer count 减一。
3. 降到零时立即释放。
4. 只有最终输出、明确 materialize 的结果和显式 pin 的 runtime feature保留。

同时增加：

- `memory_budget_mb`
- `estimated_peak_bytes`
- 根据预算自动选择 chunk size
- 分钟频率默认使用 chunk 的策略
- `clear_runtime()` 和 `release(key)` 作为过渡 API

不要先通过 `workers > 1` 解决吞吐问题。当前每个 worker 都可能持有大数组，并行会优先放大内存峰值。

### FIX-008：Executor 没有 DAG，公共子表达式会重复执行

当前 Executor 递归执行 Expr 树，只对叶子做 cache。如下公式中的相同子树会执行两次：

```python
add(
    multiply(stk.1d.raw, 2),
    multiply(stk.1d.raw, 2),
)
```

这同时影响：

- 计算时间
- 中间数组数量
- 峰值内存
- 后续融合的可行性

#### 修复建议

在 Planner canonicalize 阶段为节点生成结构签名：

```text
operator
child node ids
normalized kwargs
ValueSpec
```

结构相同且 pure 的节点合并为一个 DAG 节点。带外部状态、随机性或不可安全共享的算子不得 CSE。

优先实现 CSE，再做 fusion。没有 DAG 时直接融合容易造成重复计算或错误复用。

### FIX-009：算子融合前置条件尚不具备

#### 建议的融合顺序

1. 常量折叠。
2. 公共子表达式消除。
3. 同 source/table/scope 的批量取数。
4. 最后一次消费时的 buffer reuse。
5. 纯 elementwise 子图融合。

#### 第一阶段允许融合的候选

- add/subtract/multiply/divide
- neg/abs
- 比较算子
- mask_and/mask_or/mask_not
- where/apply_mask
- 经验证后加入 log/sqrt 等纯逐元素函数

#### Fusion barrier

以下算子初期不参与融合：

- rolling 和 delay
- cs/group/member 统计
- resample
- step reduction
- 股票到转债映射
- 指数选择和广播
- 任何依赖 sample mask 注入位置的复杂算子

#### 后端选择

不要预先绑定实现。建议使用代表性 workload 比较：

- NumPy + `out=` buffer reuse
- NumExpr 可选后端
- Numba 生成或预定义 kernel

验收指标至少包括：

- wall time
- peak RSS
- JIT/编译耗时
- 数值和 NaN 一致性
- debug 可解释性

如果简单 buffer reuse 已经能显著降低峰值内存，不必为了“有融合”而引入动态代码生成。

### FIX-010：批量取数能力不足

当前 Reader 接口一次读取一个 SourceSpec。多个字段来自同一表、相同资产、频率和 scope 时，会分别：

- 执行 SQL/parquet scan
- 构造 DataFrame/NumPy 结果
- 做日期和资产对齐

对于宽表和分钟 parquet，这类 I/O 重复可能比算术计算本身更昂贵。

#### 修复建议

增加可选批量协议：

```python
reader.read_many(specs, store, scope)
```

Planner/ExecutionSession 将可合并输入按以下维度分组：

```text
provider
table/path
asset
freq
scope
兼容的 reader params
```

Reader 一次投影多个字段，并返回 `dict[key, FeatureArray]`。不能合并的 source 自动回退到单字段 `read_source()`。

该优化应早于复杂算子融合进行。

### FIX-011：测试覆盖不足以支撑 Planner 和执行模型改造

当前 pytest 只有 4 个测试，覆盖：

- dotted key parser
- memory source + output mask
- Store import round trip
- 一个注册依赖物化

尚未系统覆盖：

- alias 保存、加载、覆盖、删除和 rename
- Store/source 同 key
- Fundamental bridge 和参数绑定
- Planner 资产/频率对齐矩阵
- input/sample/output mask
- delay_dict
- chunk 与 full execution 等价性
- overlap 推断
- runtime feature 生命周期
- CSE 和节点释放
- 多 source cache 和 scope 隔离
- 失败后的 staging 清理

在修改 Planner 前，应先补 characterization tests，记录现有预期行为；对于已经确认错误的行为，测试应直接描述正确目标，而不是固化错误结果。

### FIX-012：helper 自动生成的 key 没有成为一等交互结果

#### 当前行为

`get_lf()`、`get_hf()` 和 `get_fund()` 都会返回 `FeatureDef`，因此生成的 key 技术上已经存在于：

```python
feature_def.key
feature_def.dependencies
```

例如：

```python
revenue = get_fund(
    "Revenue",
    column_name="value",
    quarters=4,
)

revenue.key
# "stk.1d.Revenue_value_4Q"

revenue.dependencies
# ("stk.1d.Revenue_value_4Q",)
```

但当前交互路径没有突出这两个结果。用户在定义下游因子时通常仍然需要手工写：

```python
FeatureDef.from_key(
    "stk.1d.revenue_growth",
    formula="divide(stk.1d.Revenue_value_4Q, stk.1d.Revenue_value_1Q)",
)
```

这会带来以下体验问题：

1. 用户需要记住 helper 的命名规则。
2. 用户容易混淆 output key 和 raw source dependency key。
3. 默认参数省略、季度后缀、`data_code` 和 `publ_date_limit` token 容易拼错。
4. 完整 dotted key 即使写错，通常也要到 materialize/取数阶段才报错。
5. 指定 `name` 后，用户可能误以为 raw source key 也被改名。

所以问题不是“helper 没有生成或保存 key”，而是生成结果没有成为后续定义可以直接、安全复用的一等引用。

#### 近期修复建议

第一步不需要改变核心表达式模型，可以先改善 discoverability：

1. 所有文档示例都先保存 helper 返回值，再注册：

   ```python
   revenue_4q = manager.register(
       get_fund(
           "Revenue",
           column_name="value",
           quarters=4,
           name="revenue_4q",
       )
   )
   ```

2. 为 FeatureDef 提供紧凑的 `describe()` 或 notebook repr，明确展示：

   ```text
   output_key
   alias
   raw_dependencies
   source params
   materialize policy
   ```

3. 增加：

   ```python
   manager.explain(feature_def_or_key)
   ```

   返回 canonical output key、alias、公式依赖和绑定后的 raw source key。

4. 注册时增加可选的 eager dependency validation。完整 key 如果既不是已注册定义、Store feature，也不能被 DataRouter 解析，应在 register/validate 阶段报错，并给出 `DataRouter.search()` 的候选。

#### 推荐的可组合引用 API

中期应允许直接引用 helper 返回对象，而不是重新输入字符串。例如提供公共 `ref()`：

```python
revenue_4q = manager.register(
    get_fund("Revenue", column_name="value", quarters=4)
)

revenue_1q = manager.register(
    get_fund("Revenue", column_name="value", quarters=1)
)

formula = OpExpr(
    "divide",
    (ref(revenue_4q), ref(revenue_1q)),
    {},
)
```

其中：

```python
ref(value: FeatureDef | FeatureKey | str) -> FeatureExpr
```

应当：

1. 接受 FeatureDef，直接使用其 canonical `key`。
2. 接受 FeatureKey。
3. 兼容完整 key 字符串。
4. 不接受裸字段名。
5. 在注册阶段继续把引用冻结为 canonical key。

为了避免 `schema.py` 反向 import `engine.py`，不建议直接在 `FeatureDef` 上实现返回 `FeatureExpr` 的 `.ref` 属性。可以把 `ref()` 放在表达式构造模块，并作为公开 API 导出。

如果暂时不建设 expression builder，alias 仍可以作为人类可读的过渡方案：

```python
revenue_4q = manager.register(
    get_fund(
        "Revenue",
        column_name="value",
        quarters=4,
        alias="revenue_4q",
    )
)
```

但这依赖 FIX-002 先把 AliasRegistry 的事实来源收敛。自动生成 alias 不合适，因为不同定义可能发生难以解释的命名冲突。

#### API 使用原则

- `name`：用户希望稳定控制 output feature key 时显式指定。
- 自动 raw source name：框架根据字段和取数参数生成，用户不应手工重建。
- `alias`：面向公式作者的人类可读符号，不参与 source identity。
- `ref(feature_def)`：面向程序化组合的安全 canonical 引用。
- `describe/explain`：面向 notebook 和排错的可发现性入口。

#### 验收标准

- 用户可以在不手写自动生成 key 的情况下组合两个 helper 定义。
- notebook 中执行 helper 后可以直接看到 output key 和 raw source key。
- 指定 `name` 后，界面明确显示 output key 与 raw dependency 不同。
- 拼错的完整 key 可以在注册或显式 validate 阶段报错，而不是等到大规模计算时才失败。
- 自动命名规则只有 helper/source naming 函数实现，用户代码不复制该规则。

## 4. 建议的近期模块边界

这不是远期重写方案，而是可以从当前对象逐步演进出的近期边界：

| 对象 | 负责 | 不负责 |
| --- | --- | --- |
| DefinitionCatalog | canonical FeatureDef 注册、查询、依赖关系 | alias、取数、执行策略 |
| AliasRegistry | alias 到 canonical key 的解析和持久化 | 定义内容、runtime Expr |
| SourceCatalog | key/SourceRef 到 SourceSpec 的解析 | Store 优先级、数组生命周期 |
| Planner | 绑定、空间推断、改写、验证、lower | 执行 I/O、持有大数组 |
| ExecutionSession | 执行 DAG、cache、生命周期和内存预算 | 修改定义、解析 alias |
| FeatureStore | Snapshot 轴、物化数据和 metadata | 猜测外部 source |
| MaterializationRequest | overwrite/chunk/workers/budget 等运行策略 | 因子计算语义 |

## 5. 分阶段实施路线

### Phase 0：正确性基线

目标：先固化 source 复用契约，并消除已确认的 alias 非一致行为。

工作项：

1. 增加 Store/source 同 key、相同参数时复用物化结果的契约测试。
2. 增加 source 参数变化时自动生成的 raw source key 变化并重新拉取的契约测试。
3. 增加 alias overwrite 陈旧绑定测试。
4. 修复 definition overwrite 时旧 alias 未移除。
5. 为 FeatureDef 增加 key 字段一致性校验。
6. 文档示例统一展示 helper 返回对象的 `.key` 和 `.dependencies`。
7. 增加 helper 自动 key 与显式 `name` 的行为测试。
8. 增加 full/chunk 等价性测试。
9. 记录当前代表性因子的运行时间和峰值内存。

完成条件：

- 相同 raw source key 的 Store-first 复用被测试明确保护。
- 参数不同的 source 自动生成不同 raw source key。
- alias overwrite 后不再残留旧绑定。
- 后续 Planner 改造有稳定测试基线。

### Phase 1：命名和输入绑定

目标：让每个叶子的来源在执行前确定。

工作项：

1. 引入 AliasRegistry。
2. 将 FeatureDef.alias 降级为兼容输入。
3. 增加公共 `ref(FeatureDef | FeatureKey | str)`，允许直接组合 helper 返回对象。
4. 增加 `manager.explain()` 和 eager dependency validation。
5. 引入 input binding 表或 BoundInput 节点。
6. Executor 按参数化 raw source key 绑定为 materialized source 或 external source，不再临时猜测。
7. Fundamental 改用通用 SourceRef/source_inputs。
8. DataRouter catalog 扫描改为 lazy。

完成条件：

- debug plan 显示每个输入的 origin、raw source key 和 SourceSpec。
- 删除 Fundamental 专用 Manager bridge。
- 已经生成的计划不会在执行中改变输入来源。

### Phase 2：Planner 可审阅性

目标：Planner 的每一步都可以独立 review 和测试。

工作项：

1. 引入 PlanResult 和 ValueSpec。
2. 拆分显式 passes。
3. 把算子空间、mask 和 overlap 语义收口到 OperatorSpec。
4. 增加 plan trace/golden tests。
5. 为所有跨资产、跨频率规则建立表驱动测试。

完成条件：

- 任意公式可以打印 parse、bind、align、mask 后的计划。
- Planner 不通过隐藏可变计数器表达诊断结果。
- 新增普通算子不需要同时修改多个硬编码集合。

### Phase 3：内存生命周期

目标：内存占用可预测、可限制。

工作项：

1. 引入 ExecutionSession。
2. lower Expr 为 DAG。
3. consumer-count release。
4. source cache 和 runtime cache 纳入 session。
5. 增加内存估算和预算。
6. 自动选择 chunk size。

完成条件：

- plan 可以报告预计峰值。
- execution 可以报告实际 peak/live bytes。
- 非输出中间结果在最后一次消费后释放。
- 低内存预算下能够自动缩小 chunk，而不是直接 OOM。

### Phase 4：低风险优化

目标：在不改变数值语义的前提下去除明显冗余。

工作项：

1. constant folding。
2. CSE。
3. batch source read。
4. `out=` buffer reuse。
5. 根据 DAG 调整二叉子树执行顺序以降低峰值。

完成条件：

- 数值结果与基线一致。
- source scan 次数、执行节点数和峰值内存可量化下降。

### Phase 5：算子融合实验

目标：验证 elementwise fusion 是否值得进入默认执行路径。

工作项：

1. 标记 pure elementwise operator。
2. 识别 barrier 之间的可融合子图。
3. 实现一个可关闭的实验 backend。
4. 对日频、分钟和 mask 密集公式分别 benchmark。

完成条件：

- 默认仍可回退到 NumPy Executor。
- fusion plan 可打印、可解释。
- 对代表性 workload 有稳定收益，而不是只优化微基准。

## 6. 建议测试矩阵

### Helper key 与引用

- `get_lf/get_hf/get_fund` 自动 output key
- `get_fund` 参数变化生成不同 raw source key
- 显式 `name` 只改变 output key
- `ref(FeatureDef)` 使用 canonical output key
- `manager.explain()` 同时显示 output 和 raw dependency
- 拼错 key 在 eager validation 阶段失败并给出候选

### Definition 和 alias

- register 新定义
- overwrite 同 key、相同 alias
- overwrite 同 key、不同 alias
- alias 指向缺失 key
- save/load round trip
- rename 未物化定义
- rename 已物化定义明确失败

### Source binding

- source only
- store only
- runtime only
- source 与 store 同 raw key 时复用 Store
- source 参数变化时生成不同 raw key 并重新读取
- output 与 source 同 key、`overwrite=True` 时保持 source 复用契约
- 显式 refresh source 与 overwrite target 的行为相互独立
- plan 生成后 Store 状态变化不改变该计划的绑定
- Fundamental 不同 quarters/data_code/publ_date_limit

### Planner

- 同资产同频率
- 1d 到 intraday delay
- 粗分钟到细分钟 ffill
- 细分钟到粗分钟显式 resample
- stk 到 cb
- idx 显式广播
- 三类 mask
- 非法频率和资产组合

### Chunk 和生命周期

- full/chunk 结果等价
- delay overlap
- rolling overlap
- 多层 runtime dependency overlap
- 最后消费者完成后节点释放
- memory budget 自动降 chunk
- 执行失败后 cache 和 staging 清理

### 优化

- 相同子树只执行一次
- 非 pure 节点不做 CSE
- batch read 与单字段 read 等价
- buffer reuse 前后 NaN、dtype、missing_value 一致
- fusion backend 与 NumPy backend 一致

## 7. 可观测性建议

建议为 debug 和 benchmark 输出统一的执行摘要：

```text
plan_nodes
bound_inputs
source_reads
source_bytes
cache_hits
cache_misses
live_arrays
live_bytes
peak_bytes
chunk_size
overlap
fused_groups
released_nodes
```

这组指标既能帮助 review Planner，也能判断某项优化是否真的有效。

## 8. 暂不建议优先实施的事项

在 Phase 0～3 完成前，暂不建议优先做：

1. 直接增加 chunk workers。
2. 在递归 Executor 上实现复杂动态融合。
3. 仅通过拆分 `engine.py` 文件解决 Planner 可读性。
4. 用 mmap 替代生命周期分析。
5. 允许物化 feature 随意 rename。
6. 为每种新业务 source 继续向 Manager 添加专用 bridge。

这些措施可能改变代码形态或短期吞吐，但不会解决输入语义、计划表示和资源所有权问题。

## 9. 当前验证状态

本次走查执行了：

```text
pytest: 4 passed
```

并使用最小内存 source 验证了：

1. Store 会优先复用同 key source 的已物化结果；该行为符合当前 Snapshot 设计目标。
2. definition overwrite 后旧 alias 会残留；该行为需要修复。

FIX-001 后续需要补充的是 `fundamental_name()` 的参数组合回归测试，以及“指定 `name` 只改变 output name、不改变 raw source dependency”的测试。

Ruff 当前还报告：

- `core/engine.py` 一个未使用 import。
- 示例 notebook 两个 import-order 问题。

这些 lint 问题不是本轮架构优化的阻塞项，但可以在 Phase 0 一并清理。

## 10. 最终建议

近期最值得投入的四个交付物是：

1. **可发现、可组合的 helper 引用**：通过 `ref(FeatureDef)`、`manager.explain()` 和提前依赖校验，让用户无需手工重建自动 key。
2. **显式输入绑定与参数化 source naming**：保留 Store-first 复用语义，让 Planner 展示 raw source key，并移除 Fundamental bridge。
3. **可检查的 PlanResult/DAG**：让 Planner 规则、输入空间、overlap 和诊断能够被 review。
4. **ExecutionSession 生命周期模型**：让高频计算的峰值内存可估算、可限制、可释放。

完成这三项后，CSE、批量取数、buffer reuse 和 elementwise fusion 都可以作为局部优化逐步加入，而不需要再次改变公式语义和主执行链。
