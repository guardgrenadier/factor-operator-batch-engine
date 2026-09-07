# FeatureRegistry、ExecutionRequest 与 SourceExpr 设计

- 状态：第一阶段已实现
- 日期：2026-07-30
- 范围：当前 `core/` 的近期修复与能力扩展

## 1. 背景

当前项目以脚本化因子计算为主要使用方式，核心调用链为：

```text
FeatureManager
  -> FeatureDef 注册与 alias 管理
  -> 依赖递归
  -> Calculator
       -> parse / plan / execute
       -> 可选写入 FeatureStore
```

当前实现存在以下相互关联的问题：

1. `FeatureDef` 同时保存计算定义、研究命名和执行策略。
2. alias 同时存在于 `FeatureDef.alias`、`FeatureManager.aliases`、`aliases.json`、
   物化 metadata 和 `Calculator.aliases` 中，没有唯一事实来源。
3. `dependencies` 既可由调用方传入，又会从公式收集，存在两份事实来源。
4. `get_fund()` 使用 `dependencies` 充当特殊标记，再由
   `FeatureManager._register_fundamental_source()` 从 `params/metadata` 反向重建
   `SourceSpec` 并临时注册到 `DataRouter`。
5. `Calculator.cal_formula()` 同时负责计算、构造运行时定义、管理 alias 和写 Store。
6. 项目需要增加不经过研究定义层的多行公式计算能力。

本轮设计以修复这些边界为目标，不建设完整的定义治理、版本管理或研究资产平台。

## 2. 设计目标

本轮目标为：

1. 从 `FeatureDef` 中移出执行策略，引入 `ExecutionRequest`。
2. 引入 `FeatureRegistry`，集中管理定义、alias、规范化和依赖索引。
3. 使 alias、definition 和 dependencies 各自只有一个事实来源。
4. 引入 `SourceExpr`，让参数化外部输入在公式中显式表达。
5. 删除 fundamental 对 dependency flag 和 Manager bridge 的依赖。
6. 让 Manager 成为统一调度入口。
7. 让 Calculator 只负责计算并返回数组结果，不决定物化。
8. 为未来的多行公式直接计算入口建立清晰的命名空间边界。

## 3. 非目标

本轮不包含：

- definition 版本、修订、作者、标签和审批；
- 历史 definition 或 alias 数据迁移；
- 自动迁移既有 Store metadata；
- definition key 的通用 rename/migration；
- 跨任务计算缓存；
- 自动合并不同 key 的等价定义；
- 完整的多行公式 DAG、CSE 和多进程实现。

项目当前尚未作为长期持久化研究工具使用，因此本轮可以直接调整定义文件和 Store
metadata 的契约，不提供旧格式迁移流程。

## 4. 总体边界

目标调用链如下：

```text
研究入口
  helper / 当前兼容的 FeatureDef.from_key()
        |
        v
FeatureRegistry
  - 注册和规范化 FeatureDef
  - 管理当前 alias
  - resolve alias
  - 自动收集依赖和 source inputs
        |
        v
Manager
  - 接收 ExecutionRequest
  - 展开 definition dependencies
  - 调度 Calculator
  - 根据 materialize 决定是否写 Store
        |
        v
Calculator
  - parse / plan / execute
  - 本轮仍允许 Executor 从 Store/DataRouter 读取输入
  - 返回计算结果
```

未来直接计算入口不要求构造或注册 `FeatureDef`：

```text
多行公式 + request-local symbols + execution domain
        |
        v
Manager
  -> Compiler / Calculator
  -> arrays
```

研究入口和直接计算入口复用同一个计算内核，但是否使用 `FeatureRegistry` 由入口决定。

## 5. FeatureDef 的近期模型

### 5.1 字段分类

本轮只从 `FeatureDef` 拆出执行策略，不引入 definition 版本或 annotation 模型。

研究语义包括：

- `key`
- `alias`
- `params`
- `metadata`

计算语义包括：

- `formula`
- `steps`
- `input_mask`
- `sample_mask`
- `output_mask`
- `delay_lf`
- `delay_dict`

派生信息包括：

- `dependencies`

执行策略不再属于 `FeatureDef`：

- `materialize`
- `overwrite`
- `chunk_size`
- `overlap`
- `return_array`

### 5.2 params 和 metadata 的约束

`params` 和 `metadata` 可以继续记录 helper 的构造参数和展示信息，但执行链不得依赖
它们才能恢复完整计算语义。

特别是：

- fundamental 的 `SourceSpec` 必须进入 `SourceExpr`；
- 不能再根据 `metadata["helper"] == "get_fund"` 决定取数行为；
- 删除或修改非计算 metadata 不得改变公式执行结果。

### 5.3 dependencies 的约束

调用方和 helper 不再填写 `dependencies`。它只能由 `FeatureRegistry` 在公式完成解析和
alias resolve 后自动收集。

因此，未来的定义入口不再暴露：

```python
FeatureDef.from_key(..., dependencies=...)
```

注册后的 `dependencies` 是派生结果，不是调用方输入。

### 5.4 signature

本轮取消 `FeatureDef.signature` 和 `FeatureMeta.feature_def_signature`。

definition replacement 和等价计算检查不依赖持久化 hash。需要比较时，对规范化后的计算
字段进行结构比较。

## 6. FeatureDef 与 alias 的过渡和目标形态

### 6.1 本轮兼容形态

为了避免立即破坏现有脚本，本轮允许继续使用：

```python
FeatureDef.from_key(
    "stk.1d.alpha",
    alias="alpha",
    formula=...,
)
```

当前阶段：

- `alias` 暂时保留在 `FeatureDef`；
- 每个 definition 只能有零个或一个 alias；
- `FeatureRegistry` 根据 `FeatureDef.alias` 建立内存 alias 索引；
- alias 索引只是派生索引，不是第二份持久化事实来源。

### 6.2 未来目标形态

长期目标是不再让用户直接通过 `FeatureDef.from_key()` 创建研究定义。

未来由专门的定义入口对象负责构造和注册，例如：

```python
registry.define(
    key="stk.1d.alpha",
    formula=...,
    alias="alpha",
)
```

或由独立的 definition builder/factory 构造后交给 Registry。具体对象名称在实现对应阶段
再确定。

在该目标形态下：

- canonical definition 与 alias 正式分离；
- alias 由定义入口和 Registry 管理；
- 底层 `FeatureDef` 不再要求承载 alias；
- 用户不再直接依赖 `FeatureDef.from_key()` 的字段布局；
- `FeatureDef.from_key()` 可以降级为内部或兼容 API。

这是一项明确的未来 breaking change。本轮不实施该 breaking change，但实现
`FeatureRegistry` 时不得建立新的、难以拆除的 alias/definition 耦合。

## 7. alias 契约

### 7.1 基数约束

alias 与 definition 在当前阶段满足一对零或一关系：

```text
一个 definition key -> 0 或 1 个 alias
一个 alias          -> 恰好 1 个已注册 definition key
```

不允许：

- 一个 key 拥有多个 alias；
- 一个 alias 指向多个 key；
- alias 指向另一个 alias；
- alias 指向任意表达式；
- alias 指向 Registry 中不存在的 definition。

### 7.2 唯一事实来源

本轮 alias 的唯一持久化事实来源是 `FeatureDef.alias`。

`FeatureRegistry._alias_to_key` 是由当前 definitions 派生的内存索引。

不再使用：

- 独立的 `aliases.json`；
- Store metadata 恢复 alias；
- `Calculator.aliases` 作为研究 alias 注册表。

### 7.3 基础操作

FeatureRegistry 提供：

```python
registry.add_alias(key, alias)
registry.update_alias(key, alias)
registry.remove_alias(key)
registry.resolve_key(key_or_alias)
```

约束如下：

- `add_alias()` 要求 definition 当前没有 alias；
- `update_alias()` 要求 definition 当前已有 alias；
- `remove_alias()` 要求 definition 当前已有 alias；
- alias 修改原子更新对应 `FeatureDef` 和派生索引；
- 发生冲突或校验失败时，definition 和 alias 索引都保持原状。

### 7.4 alias 冻结

alias 只在研究定义注册阶段解析。

注册并规范化后的 formula、mask 和 `delay_dict` 中不得保留 `AliasExpr`。后续修改 alias
不会改变已经注册的其他 definitions。

Calculator 和 Runtime 不重新解析 Registry alias。

## 8. FeatureDef 序列化和 Store 边界

`FeatureDef` 只保留一套：

```python
FeatureDef.to_dict()
FeatureDef.from_dict()
```

不引入两种序列化视图。

契约如下：

1. FeatureRegistry 中的 definitions 是当前有效定义和 alias 的唯一事实来源。
2. FeatureStore 可以保存物化时使用的完整 `FeatureDef`，但它只是历史快照。
3. Store metadata 中的旧 alias 不参与当前 alias 解析。
4. Registry 初始化时不扫描 Store 恢复 definitions 或 aliases。
5. Registry 查找失败时不回退到 `store.load_feature_def()`。
6. 修改 Registry alias 不要求改写过去已经物化的 Store metadata。

因此需要删除当前行为：

```text
FeatureManager._restore_materialized_aliases()
FeatureManager.resolve() -> store.load_feature_def() fallback
```

Store 继续负责数组和物化 metadata，不负责当前研究定义发现。

## 9. FeatureRegistry

### 9.1 内部状态

```python
class FeatureRegistry:
    _definitions: dict[str, FeatureDef]
    _alias_to_key: dict[str, str]
```

内部集合不得作为可变对象直接暴露给 Manager 或用户。

### 9.2 definition 能力

FeatureRegistry 负责：

```python
registry.register(feature_def)
registry.replace(feature_def)
registry.get(key)
registry.resolve(key_or_alias)
registry.resolve_key(key_or_alias)
registry.contains(key)
registry.list(...)
registry.search(...)
registry.remove(key)
```

其中：

- `register()` 在 key 已存在时失败；
- `replace()` 要求 key 已存在，并原子替换 definition 和 alias 索引；
- `get()` 只接受 canonical key；
- `resolve()` 接受 canonical key 或 alias；
- Registry 不查询 FeatureStore。

### 9.3 注册规范化

注册时按以下顺序处理：

```text
校验 key 及 asset/freq/name 一致性
  -> parse formula 和 masks
  -> 使用当前 Registry resolve alias
  -> normalize delay_dict
  -> 收集 definition dependencies 和 source inputs
  -> 检查当前可识别的 definition 循环
  -> 校验 alias 基数和唯一性
  -> 原子写入 definition 和 alias 索引
```

FeatureRegistry 负责保证注册结果满足约束，但公式解析和 AST 变换应复用公共
Parser/Compiler pass，不在 Registry 中形成第二套表达式语义。

### 9.4 依赖索引

FeatureRegistry 可以提供：

```python
registry.dependencies_of(key)
registry.dependents_of(key)
```

这些索引全部从规范化公式派生。

Registry 不负责递归执行依赖；依赖执行由 Manager 调度。

### 9.5 不负责的能力

FeatureRegistry 不负责：

- DataRouter 和数据库位置解析；
- 数组读取和缓存；
- domain alignment；
- chunk、worker 和内存预算；
- Calculator runtime cache；
- 物化写入和 overwrite；
- 多行公式中的局部变量；
- 跨任务计算复用。

## 10. SourceExpr

### 10.1 引入原因

当前 `get_fund()` 的 dependency 同时承担两种职责：

1. 表示输入 key；
2. 充当“需要 Manager 注册 fundamental source”的 flag。

这使 Manager 必须通过 helper metadata 和 params 反向恢复真正的取数语义。

本轮明确引入 `SourceExpr`，使外部输入及其取数规格直接成为公式的一部分。

### 10.2 数据结构

拟议结构：

```python
@dataclass(frozen=True)
class SourceExpr(Expr):
    spec: SourceSpec

    @property
    def key(self) -> str:
        return self.spec.key
```

`SourceExpr` 必须支持：

- AST 序列化和反序列化；
- dependency/source input 收集；
- planner/compiler 识别；
- Executor 通过 DataRouter 显式读取。

### 10.3 FeatureExpr 与 SourceExpr

两者语义不同：

```text
FeatureExpr(key)
    引用另一个 definition，或引用待绑定的普通逻辑数据 key

SourceExpr(SourceSpec)
    显式声明一个带完整取数规格的外部 source
```

这项区分解决了 `get_fund()` 默认 output key 与 raw source key 可能相同的问题。即使两个
字符串 key 相同，节点类型也能表明一个是输出 definition，另一个是外部输入。

### 10.4 get_fund

`get_fund()` 直接构造完整 SourceSpec：

```python
spec = SourceSpec.from_key(
    raw_key,
    source="Fundamental",
    field=field,
    params={
        "column_name": column_name,
        "quarters": quarters,
        "data_code": data_code,
        "publ_date_limit": publ_date_limit,
    },
)

expr = SourceExpr(spec)
```

`get_fund()` 不再手工填写 dependencies。

Registry 从公式中收集该 `SourceExpr`。本轮由 Executor 识别该节点，并直接要求
DataRouter 根据 `SourceSpec` 读取。Manager 不再通过 helper metadata 介入。

### 10.5 DataRouter

DataRouter 增加显式接口：

```python
data_router.read_spec(spec, store, scope=...)
```

参数化 source key 的复用契约保持不变：

```text
FeatureStore 中已有相同 raw source key
  -> 复用 Store

否则
  -> DataRouter 按 SourceSpec 读取外部 source
```

影响取数结果的参数必须进入 `SourceSpec` 和参数化 raw source identity。

### 10.6 删除 bridge

完成 SourceExpr 接入后删除：

- `FeatureManager._register_fundamental_source()`；
- fundamental 对显式 dependencies 的依赖；
- `metadata["helper"] == "get_fund"` 的执行分支；
- Manager 到 `DataRouter.register_source()` 的临时 bridge；
- 使用 dependency 充当 source 类型 flag 的逻辑。

## 11. 自动依赖收集

公式完成 alias resolve 后，统一收集输入。收集结果只由公式结构决定，不依赖当时的
Registry 注册顺序。

建议内部结果为：

```python
@dataclass(frozen=True)
class CollectedInputs:
    dependencies: tuple[str, ...]
    source_inputs: tuple[SourceSpec, ...]
```

规则如下：

1. 每个 `FeatureExpr(key)` 都记入 canonical `dependencies`，不在收集阶段根据
   Registry 中是否存在该 key 进行分类。
2. `SourceExpr(spec)`：
   - 直接记为显式 source input；
   - 不递归查找 definition；
   - 不因为 key 与当前 output 相同而形成自依赖。
3. formula 和 mask 表达式中的输入都进入统一收集过程；`delay_dict` 不引入新输入，
   其规范化 key 必须已经出现在上述 dependencies 或 source inputs 中。
4. 调用方提供的 dependencies 不再参与注册。
5. Manager 执行时再判断 dependency：
   - Registry 中存在该 key 时递归计算 definition；
   - Registry 中不存在时由 Executor 按普通逻辑 external input 读取。

公开的 `FeatureDef.dependencies` 保存全部由 `FeatureExpr` 自动派生的 canonical keys。
source inputs 由公式中的 `SourceExpr` 表达，不额外保存第二份 source 列表。

## 12. ExecutionRequest

执行策略从 FeatureDef 移入：

```python
@dataclass(frozen=True)
class ExecutionRequest:
    target: str
    materialize: bool = True
    overwrite: bool = False
    chunk_size: int | None = None
    overlap: int | None = None
    return_array: bool = True
```

使用 `materialize` 而不是 `persist`，保持现有用户语义和调用体验。

字段含义：

- `target`：单个 canonical key 或 alias；
- `materialize`：是否将目标结果写入 FeatureStore；
- `overwrite`：是否允许覆盖已有目标 feature；
- `chunk_size/overlap`：本次分块调度策略；
- `return_array`：是否组装并返回内存结果。

当前只有单 worker 实现，因此本轮不暴露不会改变行为的 `workers` 配置。出现第二种实际
执行方式后再增加并发参数。

`materialize` 和 `overwrite` 只作用于 request target。递归计算得到的注册依赖作为本次
任务内中间结果使用，不自动写入 Store；如果 Store 中已经存在相同 dependency key，
Executor 仍按既有 Store-first 规则复用它。

本轮只支持单目标请求。多目标的失败隔离、公共依赖复用和结果组织留给后续批量
`ComputeRequest` 设计，不提前放入当前研究物化请求。

```python
manager.execute(
    ExecutionRequest(
        target="alpha",
        materialize=True,
        overwrite=True,
        chunk_size=20,
    )
)
```

本轮实现优先提供 `manager.execute(ExecutionRequest(...))`，不为了兼容而增加只有参数
转发的包装层。

`ExecutionRequest` 属于 Manager 调度层，不作为计算语义传入 Calculator。

## 13. Manager、Registry 与 Calculator

### 13.1 Manager

Manager 负责：

1. 接收 `ExecutionRequest`；
2. 使用 Registry resolve target；
3. 递归展开 definition dependencies；
4. 创建计算任务；
5. 调度 full/chunk 执行；
6. 调用 Calculator；
7. 根据 `materialize/overwrite` 写入 Store；
8. 根据 `return_array` 返回结果。

Manager 不再持有独立可变的 definitions 和 aliases 字典。

为避免增加只有参数转发作用的包装方法，Manager 不再重复提供
`register/resolve/add_alias`；调用方通过 `manager.registry` 使用这些定义能力。

### 13.2 Calculator

Calculator 负责：

- 规划已经明确的计算表达式；
- 执行算子；
- 管理一次任务内的中间数组；
- 返回轻量 `CalculationResult`。

```python
@dataclass
class CalculationResult:
    key: str
    values: np.ndarray
    space: FeatureSpace
    missing_value: Any = np.nan
    diagnostics: dict[str, Any] = field(default_factory=dict)
```

`CalculationResult.values` 是 Calculator 的数组输出；`space` 和 `missing_value` 保留
Manager 物化和调用方解释数组所需的最小坐标信息。该结果不携带研究 `FeatureDef`。

Calculator 不再：

- 注册或修改研究 alias；
- 构造当前研究定义；
- 从 params/metadata 猜测 source；
- 决定 `materialize/overwrite`；
- 调用 `FeatureStore.write_feature()`；
- 把任务局部输出注册为跨请求 alias。

Calculator 可以保留任务内 cache，但其生命周期不得超出一次执行请求。

本轮采用最小输入改造：

- Executor 仍可按普通 `FeatureExpr` 从 Store/DataRouter 读取；
- Executor 遇到 `SourceExpr` 时调用 `DataRouter.read_spec()`；
- 本轮不引入 InputBinding、DataProvider interface 或额外 factory；
- 后续如需彻底分离输入绑定，再在实际出现第二种执行后端时设计对应协议。

## 14. definition replacement 和重复计算

definition replacement 和重复特征识别属于本轮需要继续优化的议题，但不阻塞
FeatureRegistry、ExecutionRequest 和 SourceExpr 的边界实现。

暂定基础行为：

```python
registry.register(definition)
```

- key 已存在时失败；
- 不自动判断是否为同一个定义。

```python
registry.replace(definition)
```

- key 必须已存在；
- 原子替换 definition 和 alias 索引；
- 不修改已有物化数组。

不同 key 使用相同计算公式是允许的。Registry 不自动合并研究定义。

后续可以通过规范化计算字段的结构比较提供：

```python
registry.is_same_computation(left, right)
registry.find_equivalent(definition)
```

该比较不使用持久化 signature，也不自动改变 register/replace 行为。批任务内相同计算的
实际复用属于 Compiler/LogicalPlan 的 CSE，而不是 Registry definition 合并。

## 15. 多行公式的命名空间约束

未来多行公式中的名称分为：

```text
Registry alias
    跨请求研究命名，只在 definition 注册阶段解析

Program local symbol
    例如 ma5、ma20，仅当前公式程序有效

Request input symbol
    例如 close，由当前直接计算请求显式绑定
```

示例：

```python
ma5 = ma(close, 5)
ma20 = ma(close, 20)
factor = ma5 / ma20 - 1
```

其中：

- `ma5` 和 `ma20` 不进入 FeatureRegistry；
- `close` 可以由 request-local symbols 绑定；
- 直接计算不要求创建 FeatureDef；
- 计算结束后不会产生新的 Registry alias；
- Calculator 不使用任务局部符号修改全局状态。

## 16. 契约测试

### 16.1 FeatureDef 和 Registry

1. `materialize/overwrite` 不再属于 FeatureDef。
2. 调用方不能提供 dependencies。
3. dependencies 从 resolve alias 后的公式自动派生。
4. 注册后的 formula、mask 和 delay 中不存在 `AliasExpr`。
5. 一个 definition 最多拥有一个 alias。
6. alias 全局唯一。
7. alias 只能对应 Registry 中已注册的 definition。
8. add/update/remove alias 同步更新 FeatureDef 和派生索引。
9. alias 操作失败时 Registry 状态不变。
10. 修改 alias 不改变其他已注册 definitions。
11. replace definition 不遗留旧 alias。
12. Registry 不从 Store 恢复 definition 或 alias。
13. Registry resolve 不回退到 Store metadata。
14. `FeatureDef` 只有一套序列化接口。

### 16.2 SourceExpr 和 fundamental

1. SourceExpr 可以序列化和反序列化。
2. `get_fund()` 公式中包含 `SourceExpr(SourceSpec)`。
3. `get_fund()` 不显式填写 dependencies。
4. fundamental 的有效取数参数全部进入 SourceSpec。
5. 参数不同产生不同 raw source identity。
6. SourceExpr key 与 output key 相同时不形成自依赖。
7. Registry 可以分别收集全部 FeatureExpr dependencies 和 SourceExpr inputs。
8. Manager 不调用 fundamental bridge。
9. Store 存在相同 raw source key 时稳定复用。
10. Store 缺失时通过 `DataRouter.read_spec()` 读取。
11. 修改 helper metadata 不改变取数结果。

### 16.3 ExecutionRequest、Manager 和 Calculator

1. 单目标 `ExecutionRequest.materialize=True` 时由 Manager 写入 Store。
2. `materialize=False` 时只返回数组，不写 Store。
3. `overwrite` 只影响 Manager/Store。
4. Calculator 执行后不会直接产生物化 feature。
5. Calculator 不接收或修改 Registry alias。
6. Manager 不重复提供只有参数转发作用的 Registry API。
7. full 和 chunk 对同一目标保持结果一致。
8. Calculator 返回不含 FeatureDef 的 `CalculationResult`。
9. Executor 可以通过 `DataRouter.read_spec()` 读取 SourceExpr。

### 16.4 replacement 和重复定义

1. 相同 key 重复 register 失败。
2. replace 显式替换相同 key。
3. replace 失败时旧 definition 和 alias 不变。
4. 不同 key 的相同计算允许注册。
5. 结构比较可以识别等价计算，但不自动合并 definition。

### 16.5 多行公式的预留契约

1. program local symbol 不进入 Registry。
2. request input symbol 不进入 Registry。
3. 直接计算可以在没有 Registry 的情况下工作。
4. 直接计算不会隐式生成 FeatureDef。

## 17. 实施顺序

建议按以下顺序实现：

```text
1. 补充当前行为和目标契约测试
2. 从 FeatureDef 移除 materialize/overwrite，新增单目标 ExecutionRequest
3. 引入 FeatureRegistry，并由 `manager.registry` 暴露定义能力
4. 收敛 alias 为 FeatureDef.alias + Registry 派生索引
5. 删除 aliases.json、Store alias restore 和 Registry 的 Store fallback
6. 将 dependencies 改为注册阶段自动派生
7. 引入 SourceExpr 及其序列化
8. 改造 get_fund 和 DataRouter.read_spec()
9. 删除 fundamental Manager bridge
10. 新增 CalculationResult，并将 Store 写入从 Calculator 移至 Manager
11. 增加 definition 结构比较和重复计算检查能力
12. 在上述边界稳定后实现多行公式入口
```

## 18. 本轮完成标准

本轮完成后应满足：

1. FeatureDef 不再携带执行策略。
2. 单目标 ExecutionRequest 使用 `materialize` 表达是否物化。
3. FeatureRegistry 是当前 definition 和 alias 的唯一管理入口。
4. 当前阶段 alias 虽保留在 FeatureDef，但不存在第二份持久化 alias 表。
5. Registry 不从 Store 恢复定义和 alias。
6. dependencies 完全由规范化公式派生。
7. fundamental 使用 SourceExpr 显式表达 SourceSpec。
8. Manager 中不存在 fundamental 专用 bridge。
9. Calculator 返回轻量 CalculationResult，不直接写 Store。
10. 直接计算入口未来可以绕过 FeatureRegistry，而不需要复制计算链路。
11. 文档明确记录未来将通过专门定义入口替代直接
    `FeatureDef.from_key()`，并正式分离 alias 与 canonical definition。
