# 当前已知 Bug

本文用于收集代码 review 过程中确认的当前实现问题。这里只记录已经能够从代码路径明确判断的问题；设计讨论和一般改进建议不列为 Bug。

## 1. `resample` 错误耦合任务的 target asset

### 状态

已修复（2026-08-05）。

### 相关代码

- `src/factor_engine/compiler.py::_lower_resample()`

### 问题描述

旧的 resample 专用 lowering 在创建 `resample` OperatorTerm 时，直接把输出 Domain 设置为：

```python
self._target_domain
```

但 resample 在语义上只应该改变输入 Term 的 frequency/steps，不应该改变 asset type、codes、calendar 或资产轴身份。

例如，任务目标是 `cb.1d`，公式先在股票分钟数据上计算并聚合：

```text
stk.5min
  -> resample(..., "1d")
  -> project_stk_to_cb(...)
  -> cb.1d
```

修复前，`resample(stk.5min, "1d")` 会被错误标记成 `cb.1d`，而实际数组的资产维仍然是股票轴。如果股票数与转债数不同，Runtime 的 shape 校验也会失败。

### 修复前行为

```text
stk.5min
  -> resample
cb.1d    # 错误地使用完整 target Domain
```

当前实现已经解除 resample 与任务 `target_freq` 的耦合，允许用户在局部子树中显式转换到中间频率；任何后续粗到细转换仍必须显式调用 `align_frequency(..., method="ffill")`。

### 已确认契约

```text
stk.5min
  -> resample
stk.1d
  -> project_stk_to_cb
cb.1d
```

resample 输出 Domain 应当：

- 保留输入 Term 的 `asset_type`；
- 保留输入 Term 的 `codes` 和 `axis_fingerprint`；
- 保留输入 Term 的 `calendar`；
- 只将 `frequency` 和 `step_count` 替换为目标频率对应值。

### 修复内容

当前 `_lower_resample()` 根据输入 Domain 构造输出 `TermDomain`，只写入目标
frequency 和对应 step_count，不复用完整任务 Domain，也不要求目标频率等于任务 target。

### 验证覆盖

- 同资产分钟数据 resample 到日频能够正常计算；
- resample Term 保留输入的 asset、codes、calendar 和 axis_fingerprint；
- 目标为另一资产且股票数与目标资产数不同时，在数据加载前因最终 Domain 不匹配而失败；
- resample 到任务目标频率后保留输入资产轴身份。

## 2. 转债正股投影依赖股票列位置，无法安全支持资产子集或重排

### 状态

已修复（2026-08-11）。

### 相关代码

- `src/factor_engine/operators/alignment.py::lookup_by_col()`
- `src/factor_engine/data_provider/datasets.py::_cb_stock_map()`
- `src/factor_engine/compiler.py::_expand_helpers()` 中的 `project_stk_to_cb` helper

### 问题描述

旧实现中，`project_stk_to_cb(values, mapping)` 会展开为：

```text
lookup_by_col(values, mapping)
```

`lookup_by_col()` 把 mapping 中的数值直接解释成 `source_values` 股票资产轴的列位置：

```python
out = source[np.arange(source.shape[0])[:, None], safe_col, :]
```

而 `_cb_stock_map()` 生成列位置时，使用的是 FeatureStore 的完整股票 master axis：

```python
stock_col = {
    code: pos
    for pos, code in enumerate(store.get_asset_codes("stk"))
}
```

如果任务通过 `asset_scope` 选择了股票子集，或者显式改变了股票代码顺序，实际 `source_values` 的列位置就不再等于 Store master axis 的列位置。此时可能读取错误股票，也可能因为列号越界而得到 NaN。

### 修复内容

正式接口改为单参数 helper：

```python
factor = project_stk_to_cb(stock_values)
```

helper 在 Source 描述前自动注入任务级 mapping：

```text
lookup_by_col(
    stock_values,
    SourceRefExpr("cb.1d.underlying_stk_col"),
)
```

正式 `SmartQuantDataProvider` 的每个实例只服务一个任务。Compiler 通过该实例解析并
冻结任务的 `stk/cb` 有序代码轴；`CBStockMap` loader 读取原始 `CB -> StockInnerCode`
关系后，直接使用同一实例的 `stk.codes` 生成位置：

```text
DataProvider 读取 CB -> 正股 InnerCode
  -> 使用当前任务 Provider 冻结的 stk.codes 生成列位置
  -> lookup_by_col(source_values, positions)
```

正股不在任务股票子集时产生 NaN。位置 mapping 不进入跨任务缓存。旧
`FeatureStoreDataProvider` 明确拒绝该 Source，避免重新启用 Store master axis 路径。
通用 `lookup_by_col()` 仍然只处理数组列位置。

### 验证覆盖

- helper 自动注册 mapping Source；
- 显式股票子集和任意代码重排；
- mapping 指向股票子集外代码时输出 NaN；
- whole-domain 与逐日 chunked 结果一致；
- 旧 Provider 在 Source 描述阶段明确失败。

## 3. Workspace 逻辑释放不能保证实际内存及时释放，峰值可能跨分区累积

### 状态

待修复。

### 相关代码

- `src/factor_engine/execution.py::Runtime.execute_partition()`
- `src/factor_engine/data/router.py::DataRouter.read_spec()`
- `src/factor_engine/data/router.py::DataRouter._cache`
- `src/factor_engine/model.py::ExecutionStats`

### 问题描述

Runtime 根据 Term 引用计数执行：

```python
workspace.pop(input_id, None)
```

这只能删除 Workspace 字典中的一个 Python 引用，不能保证对应 ndarray 及其底层 buffer 已经释放。当前执行路径中仍可能存在其他强引用：

1. `loaded` 会继续引用最后一次 `load_many()` 返回的整组 Source 数组，直到被重新赋值或分区执行结束；
2. `args` 会继续引用最近一次 operator 的输入数组；
3. `value` 会继续引用最近一次 operator 的输出数组；
4. NumPy view 会通过 `.base` 保留上游数组的底层 buffer；
5. 输出 Term 被排除在输入释放逻辑之外，会保留在 Workspace 中直到分区结束；
6. `DataRouter._cache` 按 `SourceSpec + snapshot + scope` 缓存 `FeatureArray`，不同日期分区具有不同 scope，缓存可能跨分区持续累积，而这些条目通常不会被后续分区复用。

因此当前实现不能可靠保证 Term 最后一次使用后立即回收实际内存，也不能保证使用固定 `chunk_size` 后实际峰值内存保持有界。

### 当前行为

```text
Term 最后一次被消费
  -> workspace.pop(term_id)
  -> ExecutionStats 记录 released_terms
  -> Workspace entry 数下降

但 ndarray 仍可能被：
  loaded / args / value / NumPy view / Router cache
继续引用
  -> 底层 buffer 未释放
```

`ExecutionStats.peak_workspace_values` 只记录 Workspace 中值的数量，没有统计：

- ndarray 实际 `nbytes`；
- 多个 view 是否共享同一 buffer；
- Provider/Router cache；
- `compute()` 正在装配的完整输出数组；
- ResultStream 消费者保留的 chunk。

因此该指标不能代表真实峰值内存。

### 预期行为

- 非输出中间数组在最后一个消费者完成后，不再被 Runtime 的无效局部变量或缓存持有；
- 分区结束后，当前分区的不再复用 Source 数据能够释放；
- Router cache 有明确的任务生命周期、大小上限或关闭策略；
- `stream()` 在消费者及时丢弃 chunk 时，实际内存能够近似受单个分区工作集约束；
- 运行统计能够区分 Workspace entry、数组逻辑大小和实际共享 buffer。

`compute()` 需要持有完整结果数组，其结果内存不可能通过 Workspace 释放消除；有界内存目标主要适用于 `stream()` 路径和中间工作集。

### 建议修复方向

1. Source group 校验并写入 Workspace 后，及时清理 `loaded` 引用；
2. operator 执行和引用计数更新后，及时清理 `args` 及多余的 `value` 引用；
3. ResultChunk 已取得独立结果数组后，评估在 yield 前删除对应输出 Term；
4. 为 `DataRouter._cache` 增加任务结束清理、分区级禁用或有界 LRU 策略；
5. 明确哪些内部 operator 返回 view，必要时在“保留大 base”和“复制小结果”之间作出可配置选择；
6. 增加按唯一底层 buffer 统计的实际内存 profiling，而不是只统计 Workspace entry 数。

### 建议验证场景

- 单分区内存在多个大型 Source load group；
- 长 Operator 链中间 Term 最后一次引用后的实际对象生命周期；
- `np.broadcast_to` 等 view 保留大数组 base 的场景；
- 多日期分区执行时 Router cache 是否线性增长；
- `stream()` 消费并立即丢弃 chunk 时的常驻内存；
- `compute()` 与 `stream()` 的峰值内存对比；
- 使用真实 RSS/allocator profiling 验证，而不是仅断言 `released_terms`。

## 4. Mask 运算违反 NaN 三值逻辑，Missing 被静默转换为 True/False

### 状态

P0，已修复（2026-08-05）。

### 相关代码

- `src/factor_engine/operators/elementwise.py` 中的比较算子
- `src/factor_engine/operators/elementwise.py::mask_and()`
- `src/factor_engine/operators/elementwise.py::mask_or()`
- `src/factor_engine/operators/elementwise.py::mask_not()`
- `src/factor_engine/operators/cross_section.py::_sample_mask_3d()`
- `FACTOR_ENGINE_DESIGN.md` 的 Runtime 值协议和三值逻辑

### 问题描述

设计契约规定：

```text
True    = 1.0
False   = 0.0
Missing = NaN
```

比较和逻辑运算必须保留 Missing：

```text
NaN > 0       -> NaN
not(NaN)      -> NaN
NaN AND False -> False
NaN AND True  -> NaN
NaN OR False  -> NaN
NaN OR True   -> True
```

修复前的比较算子直接使用 NumPy 比较：

```python
np.asarray(x) > np.asarray(y)
```

修复前的 Mask 逻辑算子则把输入转换为 `dtype=bool`：

```python
np.asarray(mask, dtype=bool)
np.logical_and(...)
np.logical_or(...)
np.logical_not(...)
```

NumPy/Python 会把 `NaN` 当作 truthy，并且比较 `NaN > 0` 直接得到 `False`，从而丢失 Missing 状态。Runtime 后续即使把 bool 结果转换为 float64，也只能得到 `0.0/1.0`，无法恢复已经丢失的 NaN。

### 修复前行为

最小复现结果：

```text
greater([NaN, 1], 0)       -> [False, True]
mask_not([NaN, 1, 0])      -> [False, False, True]
mask_and([NaN], [1])       -> [True]
mask_or([NaN], [0])        -> [True]
```

这些结果与设计契约不一致，并且不会触发异常，属于静默数值错误。

修复前 `apply_mask(x, NaN)` 返回 `x`。当时的设计文档只规定“明确 False 才过滤，Missing 由算子契约定义”，因此不能仅凭这一点判定 `apply_mask` 自身错误；但上游比较把 Missing 错误变为 False 后，会让 `apply_mask` 错误过滤数据。

### 影响

- 停牌和可交易状态；
- 上市状态；
- 指数成分和样本集合；
- `where()`、`apply_mask()` 的上游 mask；
- 多个 mask 的组合；
- 截面 `sample_mask`；
- 所有依赖比较结果区分 Missing 与 False 的因子。

### 预期行为

比较算子输出 float64 三值 mask，并在任一比较输入 Missing 时输出 NaN。逻辑算子必须按照三值真值表计算，不能先转换为 NumPy bool。

### 已确认的消费契约

本次修复已经与需求方确认以下 Missing 语义：

- `where(mask, x, y)` 遇到 Missing mask 时输出 Missing；
- `apply_mask(x, mask)` 遇到 Missing mask 时输出 Missing；
- 截面 `sample_mask` 遇到 Missing 时按“不在样本内”处理；输出位置是否保留沿用各算子对 False sample 的契约；
- `member_*` 的动态成员 mask 遇到 Missing 时按非成员处理；
- mask 只允许 `0.0/1.0/NaN`，其他有限值或无穷值明确报错。

上述契约已经同步到 `FACTOR_ENGINE_DESIGN.md`。

### 修复内容

- 增加统一的三值 mask 规范化与合法值校验；
- 比较算子直接输出 float64 三值 mask，并保留任一输入的 Missing；
- `mask_and/mask_or/mask_not` 按完整三值真值表执行；
- `where/apply_mask/sample_mask/member mask` 按已确认契约消费 Missing；
- Runtime 对 MASK Source 和 Operator 结果执行 `0.0/1.0/NaN` 协议校验；
- 增加比较、完整二元真值表、变长逻辑、广播、mask 消费者、截面样本和 Runtime 集成测试。

### 验证覆盖

- 每个比较算子的左右输入分别包含 NaN；
- `mask_not` 的 True/False/Missing；
- `mask_and` 和 `mask_or` 的完整九种二元组合；
- 三个以上 mask 的组合；
- scalar 与数组广播；
- 三值 mask 经过 `where/apply_mask` 后的明确契约；
- Runtime 输出 dtype 为 float64 且 Missing 保留为 NaN。

## 5. 默认 Operator Registry 包含未实现 Domain Lowering 的 shape-changing 算子

### 状态

已修复（2026-08-06）。

`OperatorSpec.domain_rule` 现在负责 shape-changing operator 的输出 Domain；普通 NumPy 规则、资产 reduce、step reduce、位置选择和跨轴 lookup 均有独立契约。旧的手工广播和 step 对齐算子已退出新默认 Registry。

### 相关代码

- `src/factor_engine/operators/elementwise.py::OperatorSpec`
- `src/factor_engine/operators/registry.py::default_operator_registry()`
- `src/factor_engine/compiler.py::_lower_operator()`
- `src/factor_engine/execution.py::_validate_operator_result()`

### 问题描述

普通 `_lower_operator()` 默认把输出 Domain 设为唯一的非标量输入 Domain。Compiler 目前只为少数内部 Domain operator 和 `lookup_by_col` 编写了特殊规则。

但默认 registry 同时注册了多种会改变 step 数、资产数或数组维度的公开 operator，例如：

```text
get_step/slice_step
step_mean/step_sum/step_std/step_max/step_min
step_first/step_last/step_kurtosis/step_corr
intraday_flat_mean/intraday_flat_std
select_by_pos
broadcast_ts/broadcast_to_steps
ffill_to_finer_steps
```

这些 kernel 的实际输出 shape 会发生变化，但 TermDomain 仍记录输入 shape。Runtime 随后按照错误的 TermDomain 校验结果并失败。

设计文档原本规定“Domain 变换由 Domain Lowering 决定，不由 OperatorSpec 通用推断”。因此已确认的 Bug 是 registry 暴露能力与 Compiler Domain-lowering 能力不一致；是否给 `OperatorSpec` 增加通用 domain inference 只是修复方案之一。

### 修复前行为

输入为 `T × N × 4` 时：

```python
factor = step_mean(x)
```

Compiler 仍记录输出 `step_count=4`，而 kernel 返回 `T × N × 1`。实际错误为：

```text
RuntimeExecutionError:
Operator 'step_mean' returned shape (1, 2, 1), expected (1, 2, 4)
```

所以“算子出现在默认 registry”目前不代表它能够在新 batch pipeline 中正确执行。

### 影响

- 多个已注册算子在合法输入下必然 Runtime 失败；
- Parser 和 Compiler 接受调用，但错误延迟到数据已经加载后的 Runtime；
- operator 数量或 registry 内容会高估新引擎的实际能力；
- notebook 和调用方需要绕开已存在的 kernel，手工使用内部 Domain helper。

多数场景会 fail-fast，而不是静默产生错误值，但属于公共能力与实现不一致。

### 预期行为

任何进入新引擎默认 registry 的 operator 必须满足以下之一：

1. 输出 Domain 与输入 Domain 完全一致；或
2. Compiler 有明确、可验证的 Domain Lowering；或
3. 在尚未实现对应 lowering 前不对新引擎暴露。

### 建议验证场景

- 对默认 registry 中每个 operator 做契约测试；
- 编译期 TermDomain 与 kernel 实际 shape 一致；
- step reduction、step selection、resample、资产选择和广播分别覆盖；
- whole-domain 与 chunked 执行结果一致；
- 不支持的 operator 在编译期明确失败，而不是加载数据后才失败。

## 6. 动态 `sample_mask` 和 `weight` 无法作为 Term 输入进入公式 DAG

### 状态

已修复（2026-08-12）。

### 相关代码

- `src/factor_engine/formula.py::FormulaParser._convert()`
- `src/factor_engine/formula.py::OperatorExpr`
- `src/factor_engine/compiler.py::_canonical_call()`
- `src/factor_engine/operators/registry.py::default_operator_registry()`
- `src/factor_engine/operators/cross_section.py`

### 问题描述

截面 kernel 本身支持动态数组参数，例如：

```text
rank(x, sample_mask=None)
winsorize(x, sample_mask=None, lower=..., upper=...)
neutralize(x, exposure, sample_mask=None)
group_mean(x, group, sample_mask=None, weight=None)
member_mean(x, member, sample_mask=None, weight=None)
```

但 OperatorSpec 只把固定的前几个位置参数声明为 Term 输入。`_canonical_call()` 将超出的参数视为编译期配置，并要求它们必须是 `LiteralExpr`。

因此位置参数写法：

```python
rank(x, tradable_mask)
```

会把 `tradable_mask` 当作配置参数并报错：

```text
CompileError: Operator 'rank' configuration must be literal
```

关键字写法同样不可用：

```python
rank(x, sample_mask=tradable_mask)
```

Parser 要求 keyword value 必须能通过 `ast.literal_eval()`，因此报错：

```text
FormulaParseError: keyword arguments must be literals
```

当前 AST 的 `params` 也没有把表达式 keyword 作为依赖 Term 表达的完整协议。

### 影响

以下动态参数无法进入新 batch pipeline 的 Term DAG：

- `rank/winsorize/cs_*` 的 `sample_mask`；
- `neutralize` 的 `sample_mask`；
- `group_*` 的 `sample_mask` 和 `weight`；
- `member_*` 的 `sample_mask` 和 `weight`。

这会阻断随日期和资产变化的样本筛选与加权。部分场景可以预先 `apply_mask()`，但它不总是与 operator 内部 sample mask 语义等价，也无法替代动态 weight。

该问题目前在编译期显式失败，不会静默错算。

### 预期行为

公式和 Operator 契约应能明确区分：

```text
Tensor Inputs：Expr/Term 依赖，例如 x、sample_mask、weight
Literal Params：编译期参数，例如 window、lower、upper、method
```

动态 mask/weight 必须参与 DAG 依赖、Domain/ValueKind 校验、semantic identity、CSE 和引用计数。

### 修复内容

- `OperatorSpec.optional_inputs` 声明具名、可选的 Tensor Input；
- 现有 `OperatorExpr.params` 同时保留 operator 的字面量参数和动态表达式，helper 关键字仍限制为字面量；
- Compiler 将位置和关键字 Tensor Input 统一绑定到 `OperatorTerm.input_names/input_term_ids`，并纳入类型、Domain、semantic identity、CSE 和引用计数；
- Runtime 按绑定名恢复具名数组参数，再与字面量配置分开调用 kernel；
- `cs_*`、`rank`、`winsorize`、`neutralize`、`group_*` 和 `member_*` 已声明对应的 `sample_mask/weight` 输入。

### 建议验证场景

- `rank(x, sample_mask)` 的位置和关键字形式；
- `winsorize` 同时使用动态 mask 和字面量上下分位；
- `neutralize` 的 exposure 与 sample mask；
- `group_*` 和 `member_*` 的动态 mask/weight；
- 可选 Tensor Input 缺省时仍可调用；
- mask/weight Domain 不兼容时编译失败；
- 不同动态 mask/weight 必须产生不同 semantic identity。

## 7. 日期轴负 `delay` 允许未来数据泄漏，并导致 whole/chunked 结果不一致

### 状态

P0，已修复（2026-08-05）。

### 相关代码

- `src/factor_engine/operators/timeseries.py::delay()`
- `src/factor_engine/operators/registry.py::_date_delay_lookback()`
- `src/factor_engine/execution.py::PhysicalPlanner.partitions()`

### 问题描述

修复前 `delay()` 接受：

```python
delay(x, periods=-1, axis=0)
```

负 periods 会执行成 lead，即把未来日期的数据移动到当前日期。与此同时，lookback 推导使用：

```python
max(0, periods)
```

所以 `periods=-1` 得到 `job_lookback=0`，Compiler 不报告未来依赖，PhysicalPlanner 也不会读取未来分区。

### 修复前行为

输入：

```text
[1, 2, 3, 4]
```

whole-domain 执行：

```text
[2, 3, 4, NaN]
```

结果直接使用了未来数据。

设置 `chunk_size=2` 后：

```text
[2, NaN, 4, NaN]
```

分区末尾因为没有读取下一分区日期而产生额外 NaN。仅改变 `ExecutionOptions.chunk_size` 就改变最终结果，违反执行选项不得改变公式语义和数值的核心契约。

### 影响

- 因子产生未来数据泄漏；
- 回测和生产信号可能使用不可获得信息；
- whole-domain 与 chunked 结果不一致；
- 分区边界人为产生 NaN；
- job lookback 无法描述真实数据依赖。

此外，`axis=-3` 对三维 Runtime 数组实际上等价于日期轴 `axis=0`，但 `_date_delay_lookback()` 没有规范化负 axis，会错误返回零 lookback；即使 periods 为正也可能造成分块错误。

### 已确认契约

- `delay/step_delay/step_diff/step_pct_change` 的 `periods` 必须是非负整数；
- Runtime `axis` 只允许 `0=date`、`1=asset`、`2=step`，当前不支持负 axis；
- 负 periods、负 axis 和越界 axis 必须在编译期、数据加载前明确失败；
- 直接调用 operator kernel 时执行相同参数校验；
- 当前 PhysicalPlanner 只读取历史 lookback，不增加 future read horizon。

负 periods 和 future read 支持已经记录到 `todo.md`，在完成明确的 future horizon 建模前不开放。

### 修复内容

- 增加共享的三维 Runtime axis 校验，只接受 `0/1/2`；
- 增加 delay 家族统一的非负整数 periods 校验；
- 修正 `delay/ffill/ts_*` 的日期 lookback 回调，所有 axis 均先校验；
- `step_delay/step_diff/step_pct_change` 在 Compiler 阶段校验 periods，但不增加日期 lookback；
- lookback 推导错误保留具体参数原因，不再只报告笼统的推导失败；
- `delay/ffill/rolling/select_by_pos` kernel 复用同一 axis 契约；
- PhysicalPlanner 保持只读历史的现有模型。

### 验证覆盖

- `axis=0, periods<0` 编译失败；
- 所有 delay 家族的负 periods 均在加载前失败；
- `axis=-3/-2/-1` 和越界 axis 在加载前失败；
- 正向 date delay 的 whole/chunked 结果一致；
- date ffill 的 whole/chunked 结果一致；
- 非日期轴 delay 不错误增加 date lookback；
- 直接调用 operator kernel 也拒绝负 periods 和负 axis；
- 错误信息明确指出未来依赖不受支持。

## 8. `get_fund(..., quarters>1)` 与日频 step 约束冲突

### 状态

已修复（2026-08-06）。frequency 与 step_count 已拆分，`DomainSpec.target_step_count` 为独立必填字段。

### 相关代码

- `src/factor_engine/providers.py::FeatureStoreDataProvider.describe_many()`
- `src/factor_engine/compiler.py::_lower_source()`
- `src/factor_engine/data/smartquant.py::_fundamental()`
- `src/factor_engine/domain.py::get_freq_step_count()`

### 问题描述

`FeatureStoreDataProvider.describe_many()` 会把 helper 的 `quarters` 参数描述为 Source 的 `step_count`：

```python
get_fund("stk", "NetProfit", quarters=4)
```

对应的 `InputSpec` 为日频、4 steps，SmartQuant `_fundamental()` 也会实际返回 `T × N × 4` 数组。但 Compiler 的 `_lower_source()` 根据 frequency 查询固定 step 数，并要求所有 `1d` Source 都只能有一个 step：

```python
expected_steps = get_freq_step_count("1d")  # 1
if spec.step_count != expected_steps:
    raise DomainError(...)
```

因此 helper 参数能够进入 `SourceRefExpr.semantic_params` 和 Provider，却无法进入可执行 Term DAG。

### 修复前行为

编译以下公式：

```python
fund = get_fund("stk", "NetProfit", quarters=4)
factor = fund
```

会失败：

```text
DomainError:
Source 'stk.1d.NetProfit' declares 4 steps;
frequency '1d' requires 1
```

### 影响

- `get_fund()` 对 `quarters>1` 的公开参数在新引擎中实际不可用；
- Provider、Reader 与 Compiler 对 step 轴含义不一致；
- 基本面季度维和日内时间 step 被同一个 `step_count` 表示，但具有不同语义；
- 即使简单删除校验，最终输出 Domain、step operator 和频率对齐仍可能错误解释季度维。

### 已确认契约

在修复前需要明确：

- frequency 是否只表示重采样频率，而不固定决定 step_count；
- 基本面季度维是否继续复用 Runtime 第三维；
- 最终 OutputDomain 是否允许日频多 step；
- 多 step 日频数据参与日内表达式时采用何种广播或拒绝规则；
- step selection/reduction 后如何推导输出 Domain。

### 修复内容

`_lower_source()` 现在保留 InputSpec 的原始 frequency/step_count；普通 operator 只按确认的 singleton 规则合并 Domain。Provider binding、输出 metadata 和 shape-changing operator 同时使用独立 step_count，并覆盖日频多 step 的加载、计算和输出测试。
