# Reader 架构调用链 Walkthrough：从 Source Reference 到 Runtime Workspace

- 状态：当前实现走读
- 更新日期：2026-08-31
- 适用配置：`data_sources.json` schema version 3
- 重点源码：
  - `src/factor_engine/data_provider/catalog.py`
  - `src/factor_engine/data_provider/smartquant.py`
  - `src/factor_engine/data_provider/datasets.py`
  - `src/factor_engine/data_provider/normalize.py`
  - `src/factor_engine/execution.py`
- 相关背景：
  - [Reader 与 Load 规范化边界设计](Reader与Load规范化边界设计.md)
  - [数组布局与数据加载边界设计](数组布局与数据加载边界设计.md)
  - [ADR-0001：普通算子采用按位置的数组布局契约](adr/0001-use-positional-array-layout-for-operators.md)

本文以当前代码为准，专门解释 Source 的物理读取链路。公式解析、DAG lowering 和结果
装配只讲到与 Reader 相接的边界；完整公式编译过程仍可参考
[调用链设计解读](调用链设计解读_walkthrough.md)。

背景设计文档仍保留迁移前的候选表述；若其中的 `sparse_sql_panel`、
`static_relation` 或“待实现”状态与本文冲突，以本文和当前源码为准。

## 1. 先看结论：一条 Source 数据怎样进入 Runtime

```text
公式里的 Source Reference
  │
  │ Compiler -> provider.describe_many()
  ▼
Catalog.describe()
  └─ 只返回 InputSpec：asset / frequency / step_count / ValueKind
  │
  │ LogicalPlan + PhysicalPlanner 产生分区 ReadDomain
  │ Runtime -> provider.bind_many()
  ▼
Catalog.bind()
  └─ SourceSpec + ReadDomain -> SourceBinding
  │
  │ SmartQuantDataProvider 计算 load_group_key
  ▼
Load Group
  └─ 同一 Dataset、同一 ReadDomain、兼容物理查询语义的 bindings
  │
  │ Runtime 首次遇到组内 SourceTerm 时调用 load_many()
  ▼
ReaderRequest
  │
  │ READER_REGISTRY[DatasetSpec.reader](request)
  ▼
Iterator[RawBatch]
  └─ labels / flat / static 坐标 + 每个 term_id 的一维原始值列
  │
  │ LoadNormalizer.normalize()
  ▼
NormalizedSourceBatch
  └─ term_id -> 只读 float64 T × N × S
  │
  │ Runtime 只检查批次标记和 term_id 集合
  ▼
Workspace -> OperatorTerm -> ResultChunk
```

架构的核心是两次分离：

1. `describe_many()` 只回答编译期语义问题，不携带物理查询行为；
2. Reader 只返回实际物理记录，最终数组和值协议统一由 `LoadNormalizer` 完成。

## 2. 先统一几个容易混淆的对象

| 对象 | 产生位置 | 回答的问题 | 不负责什么 |
| --- | --- | --- | --- |
| `InputSpec` | `Catalog.describe()` | Source 的资产、频率、step 和 ValueKind 是什么 | 表、字段和查询方式 |
| `DatasetSpec` | Catalog 初始化 | 物理数据集由哪个 Reader、用哪些物理参数读取 | 某次任务的日期和资产范围 |
| `SourceSpec` | `Catalog.bind()` | 一个逻辑 Source 落到哪个 Dataset、字段/常量/default/投影 | 最终数组和坐标散布 |
| `ReadDomain` | PhysicalPlanner / bind 阶段 | 本分区实际读取哪些 date、asset、step | 物理表结构 |
| `SourceBinding` | `bind_many()` | 某个 SourceTerm 在本分区的 SourceSpec、ReadDomain 和 LoadGroup | 执行查询 |
| `ReaderRequest` | `load_many()` | Reader 本次需要的 Dataset、bindings、ReadDomain 和小型上下文 | 数组规范化 |
| `RawBatch` | Reader | 后端实际返回了哪些一维坐标和值列 | 缺失坐标填充、dtype 和 ValueKind |
| `NormalizedSourceBatch` | LoadNormalizer | 可以被 Runtime 信任的最终 Source 数组集合 | Operator 计算 |

当前 `SourceSpec.source` 和 `SourceSpec.table` 仍被赋值，以兼容已有模型和其他 Provider；
SmartQuant Reader 的权威路由不是这两个字段，而是：

```text
SourceSpec.dataset_id -> Catalog.datasets[dataset_id] -> DatasetSpec.reader
```

Reader 名称不会进入公式、Source Reference 或 LogicalPlan 的语义身份。

## 3. Provider 初始化：先冻结 Catalog

创建 `SmartQuantDataProvider` 时会立即创建 Catalog：

```python
provider = SmartQuantDataProvider()
```

实际顺序如下：

1. 读取 schema version 3 的 `data_sources.json`；
2. 把每条 `datasets` 配置转成 `DatasetSpec`；
3. 验证 Reader 名称、必需配置和 Dataset 依赖；
4. 为 `sql_panel` 和 `parquet_bars` 默认执行字段发现；
5. 查询 Fundamental ItemCode，并动态扩展为多个 `ranked_sql_panel` Dataset；
6. 注册 `sources` 中显式声明的逻辑 Source；
7. 计算 Catalog fingerprint，并记录 `catalog_snapshot` 诊断事件。

Catalog 自己使用但 Reader 不需要的元数据不会进入 `DatasetSpec.params`，例如：

- `asset_axis`；
- `discover_fields`；
- `fields` / `exclude_fields`；
- 字段发现使用的 `sample_date`。

因此 `DatasetSpec.params` 更接近 Reader 的最小物理配置，而不是原始 JSON 的完整副本。

### 3.1 字段发现不是 Reader

普通 SQL 宽表通过 `information_schema.COLUMNS` 发现字段；分钟 parquet 通过一个样本文件
发现字段。发现结果只用于向 Catalog 注册逻辑 Source。真正加载任务数据时，Reader 仍只
读取 LoadGroup 请求的字段。

Fundamental ItemCode 查询同样属于 Catalog expander：它发现“有哪些逻辑 Item”，然后为
每个 Item 建立 DatasetSpec 和 SourceSpec。`ranked_sql_panel` 并不知道 ItemCode 清单。

### 3.2 资产轴查询也不是 Reader

Compiler 解析任务 Domain 时调用 `provider.asset_codes()`。SmartQuant Provider 从被标记为
`asset_axis` 的日频 Dataset 查询并冻结有序 InnerCode 轴。该查询决定任务的 N 轴，不是
任何 Source LoadGroup 的数据读取，因此不经过 Reader 或 LoadNormalizer。

## 4. 编译阶段：describe 只问 Source 的语义

Compiler 收集公式中去重后的 `SourceRefExpr`，调用：

```text
Compiler.compile()
  -> provider.describe_many(source_refs)
  -> Catalog.describe(ref)
  -> Catalog._resolve(ref)
```

`Catalog._resolve()` 先按逻辑 key 查找 Source；Fundamental 同名 Item 可以再通过
`data_code` 消歧。`describe()` 最终只构造 `InputSpec`：

```text
asset_type
frequency
step_count
value_kind
calendar
```

这一步不会创建 ReaderRequest，也不会读取行情数据。Dataset 表名、字段名、selector 和
代码映射不会进入 LogicalPlan。

## 5. 分区绑定：SourceTerm 变成 SourceBinding

`PhysicalPlanner` 先根据输出日期、chunk size 和 job lookback 创建分区
`ReadDomain`。Runtime 执行每个分区前调用：

```text
Runtime.execute_partition()
  -> SmartQuantDataProvider.bind_many(plan.source_terms, partition.read_domain)
  -> Catalog.bind(term.source_ref)
```

`Catalog.bind()` 产生 `SourceSpec`，主要内容包括：

```text
dataset_id
field 或 constant
default
projection
Source 语义参数
```

Provider 再根据 SourceTerm 的原生 asset、frequency 和 step_count 构造该 Source 的
`source_domain`，并计算 `load_group_key`：

```text
hash(
  dataset_id,
  Reader 专属兼容参数,
  source_domain.dates,
  source_domain.codes,
  source_domain.steps,
)
```

### 5.1 哪些参数影响 LoadGroup

LoadGroup 只关心“能否共享同一组物理行和同一种坐标解码”：

| Reader | 额外兼容参数 |
| --- | --- |
| `sql_panel` | 有 selector 时，共享相同 selector 参数值，例如同一 `index_inner_code` |
| `ranked_sql_panel` | 相同 `quarters` 与 `publ_date_limit` |
| 其他 Reader | 当前不需要额外 per-source 兼容参数 |

`field`、`constant`、`default` 和 `ValueKind` 不拆分 LoadGroup：

- 多个 field 可以在同一次 SELECT 或 parquet scan 中投影；
- constant 依附于同一批实际物理行；
- default 只影响缺失位置的预填充值；
- ValueKind 只影响 LoadNormalizer 的值校验。

Runtime 把相同 `load_group_key` 的 SourceBinding 放进一组。第一次在拓扑序中遇到组内任意
SourceTerm 时，整组只调用一次 `provider.load_many(group)`。

## 6. load_many：只做 Reader 与 Normalizer 的编排

`SmartQuantDataProvider.load_many()` 的流程很短：

1. 确认 bindings 指向同一个 `dataset_id`；
2. 从 Catalog 取出 `DatasetSpec`；
3. 准备 Reader context：SQL 后端、DuckDB、任务资产轴、诊断函数；
4. 对有内部依赖的 Reader 补充 DatasetSpec；
5. 构造 `ReaderRequest`；
6. 从函数注册表取得 Reader，并把其 RawBatch 迭代器直接交给 LoadNormalizer。

对应的核心表达式是：

```python
LoadNormalizer(bindings, READER_MODES[dataset.reader]).normalize(
    READER_REGISTRY[dataset.reader](request)
)
```

两个内部依赖由 Provider 在这里解析，公式和 Reader 都不负责查 Catalog：

- `adjust_factor` 获得 `anchor_dataset`；
- dated `parquet_bars` 获得 `code_map_dataset`。

## 7. 当前确认的六个 Reader

| Reader | 当前数据源 | coordinate_mode | 物理形态 |
| --- | --- | --- | --- |
| `sql_panel` | ReturnDaily、CBReturnDaily、IndexQuote、行业字段、指数权重/成员 | `labels` | date + asset 的 SQL 面板，多字段/常量投影 |
| `parquet_bars` | 股票 1min/5min、转债 1min | `flat` | 按日期分区的 parquet bars 和任务级代码映射 |
| `ranked_sql_panel` | Fundamental | `labels` | date + asset + 报告期 rank 的 PIT SQL 长表 |
| `adjust_factor` | AdjustFactor | `labels` | 基于交易行的相关 as-of 查询 |
| `untradable` | Untradable | `labels` | 多个交易状态列派生一个 mask |
| `cb_stock_map` | CBStockMap | `static` | 无日期的转债到正股关系 |

这里没有 `sparse_sql_panel`。指数记录的“稀疏”和缺失默认值属于规范化语义，不是新的
物理读取形态，因此指数权重和成员已经并入 `sql_panel`。

这里也没有通用 `static_relation`。CBStockMap 既有债券类型过滤，又有任务股票轴位置
投影，首版使用具名 `cb_stock_map` 更诚实。

## 8. Reader 分支走读

### 8.1 sql_panel：普通 SQL 面板与指数成分共用

最小 Dataset 配置：

```text
必需：table, date_col, code_col
可选：trading_flag_col, selector
```

Reader 为每个有 `field` 的 binding 生成 `value_0`、`value_1` 等投影别名；只有
`constant` 的 binding 不进入 SELECT。查询的行过滤包括：

- ReadDomain 日期范围；
- ReadDomain asset codes；
- 可选交易标志；
- 可选 selector，例如 `IndexInnerCode = 3145`。

查询结果被包装为：

```python
RawBatch(
    coordinate_mode="labels",
    coordinates={"date": DataDate, "asset": InnerCode},
    values={term_id: raw_field_or_constant},
)
```

#### 指数权重和成员为什么可以共用一次查询

以 CSI300 为例：

```text
index_weight -> field = Weight, default = NaN
is_member    -> constant = 1, default = 0, ValueKind = MASK
```

SQL 只读取真实存在的指数成分行及其 Weight。Reader 对这些行投影成员常量 1；
LoadNormalizer 预先把成员完整数组填成 0，再把真实行散布为 1。于是：

```text
有成分记录：weight = 原始权重，member = 1
无成分记录：weight = NaN，member = 0
```

“稀疏”没有产生新 Reader，`default` 也没有进入 SQL。

### 8.2 ranked_sql_panel：Fundamental 的 rank 解码

最小 Dataset 配置：

```text
table, date_col, code_col,
rank_col, report_date_col, publication_date_col
```

同一 LoadGroup 还必须共享 Source 参数：

```text
quarters
publ_date_limit
```

Reader 在 SQL 中执行日期、资产、披露窗口和 rank 上限过滤，然后把：

```text
step = quarters - EndDateRank
```

作为 labels RawBatch 的显式 `step` 坐标。例如 `quarters=2` 时，rank 2 对应 step 0，
rank 1 对应 step 1。Reader 只负责这项物理 rank 解码；最终 S 轴范围和重复检查仍由
LoadNormalizer 完成。

### 8.3 parquet_bars：分钟文件与代码映射

最小 Dataset 配置：

```text
必需：path_template, code_map
可选：duckdb_threads
```

首版固定采用当前真实文件约定：

```text
bar code 列：security_code
bar step 列：start_time
映射资产列：InnerCode
映射存储代码列：SecuCode
```

这些没有现实变体的列名不再重复写进每个分钟 Dataset。

#### 静态股票代码映射

股票分钟数据先从 `SmartQuant.InnerCode_SecuCode` 一次查询当前 ReadDomain codes 的
`InnerCode -> SecuCode`，整个日期区间共用。

#### 按日期的转债代码映射

转债分钟数据使用 `cb_return_daily` 作为内部 dated map Dataset，查询：

```text
DataDate + InnerCode + SecuCode
```

parquet 行除 SecuCode 外还必须匹配文件对应日期，避免同一转债在不同日期的存储代码变化
导致错连。

#### DuckDB 内部任务表

Reader 在 DuckDB 查询中注册四个小型任务表：

| 表 | 作用 |
| --- | --- |
| `file_axis` | filename -> date_key + date_idx |
| `code_map` | 存储代码 -> InnerCode，可选 DataDate |
| `asset_axis` | InnerCode -> asset_idx |
| `step_axis` | step_value -> step_idx |

DuckDB 在扫描 parquet 时直接计算：

```text
flat_idx = date_idx * (N * S) + asset_idx * S + step_idx
```

每个 Arrow batch 随即变成：

```python
RawBatch(
    coordinate_mode="flat",
    coordinates={"flat_idx": arrow_column_0},
    values={term_id: arrow_value_column},
)
```

Reader 不物质化完整分钟 DataFrame，也不分配最终 T × N × S。整个 Arrow 流由同一个
LoadNormalizer 增量消费，所以跨 Arrow batch 的重复位置仍能被发现。

### 8.4 adjust_factor：具名 as-of 查询

最小 Dataset 配置：

```text
anchor_dataset_id
factor_table
```

Provider 根据 `anchor_dataset_id` 把日频 anchor DatasetSpec 放进 ReaderRequest context。
Reader 以 anchor 中实际交易行作为 date/asset 坐标，对每行查询不晚于 TradingDay 的最新
`RatioAdjustingFactor`；没有历史因子时 SQL 使用 1。

as-of 条件、因子字段和生效日语义固定留在具名 Reader，不建立任意 SQL DSL，也不把它们
复制成大量 JSON 参数。

### 8.5 untradable：具名业务派生查询

最小 Dataset 配置：

```text
table, date_col, code_col
```

Reader 用当前确定的 16 个交易状态列执行 OR，返回单个 `value_0` mask。Source 配置声明：

```text
ValueKind = MASK
default = 0
```

因此物理表中存在的行由 SQL 产生 0/1；完全没有记录的位置由 LoadNormalizer 保持为 0。
状态列集合属于该具名查询的业务规则，不在 JSON 中重复维护。

### 8.6 cb_stock_map：静态关系与任务轴投影

最小 Dataset 配置：

```text
bond_code_table
relation_table
```

Reader 查询当前转债轴对应的 `StockInnerCode`，并只接受指定 BondNature。它支持两种
Source projection：

| projection | RawBatch value |
| --- | --- |
| `inner_code` | 原始 `StockInnerCode` 列，不在 Reader 中强制数值转换 |
| `axis_position` | 在任务已冻结的目标股票轴中的位置；找不到时为 NaN |

`axis_position` 必须在 Reader 内完成，因为它依赖任务级轴映射；dtype、CODE 整数语义与
缺失值检查仍交给 LoadNormalizer。Reader 返回：

```python
RawBatch(
    coordinate_mode="static",
    coordinates={"asset": convertible_bond_inner_code},
    values={term_id: stock_inner_code_or_axis_position},
)
```

RawBatch 没有 date；沿日期广播是 LoadNormalizer 的职责。

## 9. RawBatch：Reader 与规范化边界之间的唯一协议

所有 Reader 都返回同一个结构：

```python
RawBatch(
    coordinate_mode="labels" | "flat" | "static",
    coordinates={...},
    values={term_id: raw_column},
)
```

固定约束：

- coordinates 和 values 的每列必须是一维；
- 所有列必须等长；
- values 必须恰好覆盖 LoadGroup 的全部 term_id；
- Reader 只返回实际存在的物理行；
- Reader 可以为实际行生成常量列；
- Reader 不填补缺失 date/asset/step；
- Reader 不转换最终 dtype，不处理 Infinity，不校验 ValueKind；
- Reader 不分配最终 T × N × S。

Reader 查询结果为空时可以不 yield。LoadNormalizer 仍会返回完整的默认值数组。

## 10. LoadNormalizer：唯一 Source 规范化边界

创建 Normalizer 时先完成四件事：

1. bindings 必须非空、属于同一 LoadGroup、共享同一个 ReadDomain；
2. term_id 必须唯一；
3. static 模式只允许 S=1；
4. 为每个 term_id 按 default 创建 float64 T × N × S，并建立 occupancy。

然后增量处理每个 RawBatch：

```text
校验 RawBatch 类型和 coordinate_mode
  -> 校验坐标键与完整 term_id 集合
  -> 转成一维列并检查等长
  -> 坐标解析为最终 position
  -> 拒绝 batch 内和跨 batch 重复
  -> 数值化为 float64
  -> Infinity -> NaN
  -> 校验 MASK / CODE
  -> scatter 到预分配数组
```

### 10.1 三种坐标怎样定位

#### labels

`date`、`asset` 和可选 `step` 分别在 ReadDomain 的轴中查位置，再计算：

```text
flat_position = date_idx * (N * S) + asset_idx * S + step_idx
```

没有 `step` 时只允许 ReadDomain 的 S=1，并使用 step 0。

#### flat

`flat_idx` 必须是有限整数，并且满足：

```text
0 <= flat_idx < T * N * S
```

它只是 Reader 提供的位置提示，不会绕过重复和范围校验。

#### static

`asset` 先映射为 N 轴位置，然后执行：

```python
array[:, asset_positions, 0] = values
```

因此一行静态关系沿整个 ReadDomain 日期轴广播，Reader 不需要制造 T 倍重复行。

### 10.2 值协议

每列统一执行：

1. 非空但不能数值化的值失败；
2. 转成 `float64`；
3. 正负 Infinity 转成 NaN；
4. MASK 的有限值只能是 0 或 1；
5. CODE 的有限值必须是整数。

finalize 再确认 term_id、shape 和 dtype，随后把数组设为只读，包装为
`NormalizedSourceBatch`。

### 10.3 失败原子性和流关闭

任一 RawBatch 失败时，Normalizer 不返回任何部分数组。其 `finally` 会关闭 Reader
迭代器，因此分钟 Arrow 流也能及时释放。已散布到 Normalizer 内部但尚未交付的数组随
本次失败一起丢弃。

MemoryDataProvider 和 RepositoryDataProvider 已经持有按最终坐标排列的稠密数组，不走
Reader；它们通过 `normalize_source_arrays()` 复用同一 dtype、Infinity、ValueKind、shape
和只读结果契约。

## 11. Runtime 为什么可以信任 Source 数组

Runtime 调用 `provider.load_many(group)` 后只做两项检查：

1. 返回值必须是 `NormalizedSourceBatch`；
2. term_id 集合必须与 LoadGroup 完全相同。

之后数组直接进入 Workspace。Runtime 不再重新扫描 Source 的 dtype、shape、Infinity、
MASK 或 CODE，因为这些已经在唯一 Source Load 边界完成。

这项信任只针对 Source。OperatorTerm 的返回值仍由 `_validate_operator_result()` 按当前
operator 契约规范和校验，最终公式结果还要裁掉 lookback 前缀并广播到 OutputDomain。

## 12. 一个完整例子：两个日频字段怎样只发一次 SQL

假设公式同时引用：

```text
stk.1d.ClosePrice
stk.1d.TurnoverVolume
```

它们都由字段发现注册到 `stk_return_daily`：

```text
describe
  -> 两个 InputSpec：stk / 1d / S=1 / numeric

bind partition
  -> 两个 SourceSpec：同一 dataset_id，不同 field
  -> ReadDomain 相同
  -> reader_compatibility 都为空
  -> load_group_key 相同

Runtime 首次遇到其中一个 SourceTerm
  -> load_many(两个 bindings)
  -> READER_REGISTRY["sql_panel"]
  -> 一次 SELECT 同时投影两个 value 列
  -> 一个 labels RawBatch，values 覆盖两个 term_id
  -> 一个 LoadNormalizer 同时散布两个最终数组
  -> 一个 NormalizedSourceBatch 放入 Workspace
```

字段复用的单位是物理查询布局和兼容行集合，不是“日频”这个语义标签。

## 13. 责任边界速查

| 问题 | 权威组件 |
| --- | --- |
| Fundamental 有哪些 ItemCode | Catalog expander |
| 任务股票/转债有序资产轴是什么 | Provider 的 Domain 解析接口 `asset_codes()` |
| 逻辑 key 对应哪个 Dataset 和字段 | Catalog |
| 哪些 SourceBinding 可以共同读取 | Provider 的 LoadGroup key |
| SQL/parquet 怎样扫描 | Reader |
| 分钟存储代码怎样映射到任务资产 | `parquet_bars` 内部物理依赖 |
| CB 正股代码怎样变成任务股票轴位置 | `cb_stock_map` |
| 缺失坐标填什么 | SourceSpec default + LoadNormalizer |
| labels/flat/static 怎样落到 T × N × S | LoadNormalizer |
| dtype、Infinity、MASK、CODE、最终 shape | LoadNormalizer |
| Source 数组是否可信 | `NormalizedSourceBatch` 标记 |
| DAG 拓扑执行和 Workspace 生命周期 | Runtime |

明确不属于 Reader 的内容：

- 任意 SQL DSL；
- Formula、Term 或 LogicalPlan 的语义身份；
- Fundamental ItemCode 发现；
- 任务资产轴查询；
- 缺失坐标补齐；
- 最终数组分配和值协议；
- 普通算子的业务坐标检查。

## 14. 新增数据源时应该改哪里

### 场景 A：现有 Dataset 增加普通字段

如果开启字段发现，通常不改代码；否则只在 Dataset 的 `fields` 或显式 `sources` 中登记。

### 场景 B：新 Dataset 符合已有物理布局

新增 DatasetSpec 配置，并按需增加显式 Source。不要复制一个只改表名的 Reader。

### 场景 C：只有缺失默认值或 ValueKind 不同

修改 Source 配置的 `default` / `value_kind`。这不是新 Reader，也不应拆 LoadGroup。

### 场景 D：出现新的物理读取形态

才在 `datasets.py` 增加一个函数 Reader，并同步：

1. `READER_REGISTRY`；
2. `READER_MODES`；
3. Catalog 的最小配置校验；
4. 必要的 `reader_compatibility()`；
5. Reader 查询/RawBatch 测试；
6. 如有新坐标模式，再扩展 LoadNormalizer——但首版应优先复用现有三种模式。

### 场景 E：复杂业务派生

只有规则无法诚实表达为普通物理字段读取时，才考虑具名 Reader。不要引入 `custom`
Reader、Reader 类继承层级或通用查询 DSL。

## 15. 阅读源码的最短路径

建议按以下顺序打开代码：

1. `model.py`：看 `DatasetSpec`、`SourceSpec`、`ReaderRequest`、`RawBatch`、
   `NormalizedSourceBatch`；
2. `data_sources.json`：看当前 Dataset 和显式 Source；
3. `catalog.py`：看配置怎样变成 DatasetSpec / SourceSpec；
4. `smartquant.py`：只看 `bind_many()` 和 `load_many()`；
5. `datasets.py`：从 `READER_REGISTRY` 反查六个函数；
6. `normalize.py`：看 `_validate_batch()`、`_positions()`、`_convert_column()` 和
   `_finalize()`；
7. `execution.py`：看 Runtime 首次遇到 SourceTerm 时怎样整组加载。

对应的集中测试是：

- `tests/test_reader_normalizer.py`：六 Reader 集合、三坐标模式和规范化边界；
- `tests/test_smartquant_provider.py`：Catalog、资产轴、SQL 合批、分钟映射和流式加载；
- `tests/test_batch_engine.py`：Runtime 必须接收 `NormalizedSourceBatch`；
- `tests/test_mask_semantics.py`：非法 MASK Source 在 Provider 边界失败。

## 16. 记住这六条即可

1. Reader 按物理读取形态复用，不按日频/高频分类；
2. Dataset 决定 Reader，Source 只决定字段、常量、default、ValueKind 和投影；
3. LoadGroup 合并共享物理行集合和坐标解码的 SourceBinding；
4. Reader 只返回实际物理行组成的一维 RawBatch；
5. LoadNormalizer 是 Source 进入 Runtime 前唯一的坐标与值规范化边界；
6. Runtime 信任 `NormalizedSourceBatch`，不重复扫描 Source 数组。
