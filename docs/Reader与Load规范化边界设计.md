# Reader 与 Load 规范化边界设计

- 状态：目标设计，待实现
- 日期：2026-08-31
- 范围：`SmartQuantDataProvider` 内部物理读取复用，以及 Source 进入 Runtime 前的唯一规范化边界
- 相关决策：[ADR-0001](adr/0001-use-positional-array-layout-for-operators.md)

## 1. 目标

新增普通字段或接入与已有物理布局相同的数据集时只修改 Catalog 配置；只有出现新的
物理读取模式或无法配置表达的业务查询时才新增 Reader 代码。Reader 只负责物理 I/O，
不再同时承担最终数组分配、ValueKind 校验和 Runtime 契约验证。

目标链路：

```text
data_sources.json
  -> Catalog：DatasetSpec + SourceSpec
  -> Reader：物理 I/O，产生 RawBatch
  -> LoadNormalizer：坐标定位、散布、dtype/missing/ValueKind 校验
  -> NormalizedSourceBatch：term_id -> float64 T × N × S
  -> Runtime Workspace
```

`DataProvider.load_many()` 继续作为公共端口，但其内部只编排 Reader 和
LoadNormalizer。Runtime 不再重复扫描已经规范化的 Source 数组。

## 2. 为什么不按“日频/高频”抽象

frequency 是数据语义，不是物理布局。同为日频的数据可能是普通面板字段、基本面
多报告期长表、稀疏指数成分或无日期静态关系；分钟数据也可能来自分区 parquet、SQL
长表或其他后端。Reader 应按“怎样定位和读取物理记录”复用，而不是按 `1d/1min`
分派。

## 3. Reader 最小协议

首版不建立 Reader 类层级，只保留函数注册表：

```text
READER_REGISTRY[reader_kind](ReaderRequest) -> Iterator[RawBatch]
```

`ReaderRequest` 包含：

```text
DatasetSpec
同一 LoadGroup 的 SourceBinding
ReadDomain
后端与任务级小型坐标映射
```

最小数据对象使用两个 frozen dataclass：

```python
ReaderRequest(dataset, bindings, read_domain, context)
RawBatch(coordinate_mode, coordinates, values)
```

所有 Reader 返回同一种 RawBatch，只通过 `coordinate_mode` 区分三种坐标载荷：

| coordinate_mode | coordinates | 当前用途 |
| --- | --- | --- |
| `labels` | `date`、`asset`、可选 `step` | SQL 面板和 ranked SQL；缺少 step 只允许 S=1 |
| `flat` | `flat_idx` | 已按 ReadDomain 算出扁平位置的分钟流 |
| `static` | `asset` | 无日期静态关系；首版只允许 S=1 并沿日期维广播 |

`coordinates` 和 `values` 中每一列都是等长的一维 ArrayLike；`values` 必须以本 LoadGroup
全部 `term_id` 为键。Reader 可以为存在的物理行投影原始字段或常量列，但不填充缺失
坐标。没有记录时可以不产生 RawBatch。

`flat_idx` 只是位置提示，不能直接视为可信结果；Normalizer 仍负责范围和跨 batch 重复
校验。RawBatch 不分配最终 `T × N × S` 数组，不转换 ValueKind，也不决定缺失值策略。
Reader 可以分批返回 RawBatch，分钟 parquet 不需要物质化完整 DataFrame。

## 4. Reader Strategy

> 本节 Reader 清单仍是讨论候选，尚未最终确认。尤其需要继续决定指数成分是否并入
> `sql_panel`，以及 CBStockMap 应使用通用 `static_relation` 还是具名 Reader；第 3 节
> RawBatch 协议和第 6.1 节 LoadNormalizer 顺序不受该选择影响。

| reader_kind | 当前数据源 | 物理布局 | 配置化边界 |
| --- | --- | --- | --- |
| `sql_panel` | ReturnDaily、CBReturnDaily、IndexQuote、行业字段 | SQL 表中的 date + code + 多字段列，原生 singleton step | 表名、坐标列、交易标志、字段和固定过滤条件 |
| `parquet_bars` | 股票 1min/5min、转债 1min | 按日期分区的 parquet，security code + step + 多字段列 | path template、字段、step 列、代码解析器、线程数 |
| `ranked_sql_panel` | Fundamental | date + code + 报告期 rank + 多字段列 | rank 列、rank 到 step 的规则、PIT 过滤参数 |
| `sparse_sql_panel` | 指数权重、指数成员 | date + code 的稀疏记录，缺失行具有明确默认值 | 过滤参数、字段或常量投影、per-source 默认值 |
| `static_relation` | CBStockMap | 无日期的 code -> related code 关系 | 关系字段、是否转换为当前任务轴位置 |
| `adjust_factor` | AdjustFactor | 带 as-of 语义的相关查询 | 查询规则留在具名 Reader，配置只提供必要参数 |
| `untradable` | Untradable | 多字段共同派生一个 mask | 派生规则留在具名 Reader，配置只提供组成字段 |

普通字段接入规则：

1. 已存在 DatasetSpec 中的新原始列：只增加/扫描 SourceSpec；
2. 新数据集符合已有 reader_kind：只增加 DatasetSpec；
3. 新物理布局：增加一个 Reader 和对应契约测试；
4. 复杂业务派生：增加具名 Reader，不提供万能 `custom` 分派，也不把任意 SQL 模板或
   表达式 DSL 塞进 JSON。

`Untradable` 如果未来把组成标志都作为普通 Source 暴露，可改由公式 helper 展开为
mask operator；在此之前继续使用 `untradable` Reader。`AdjustFactor` 的 as-of 规则和
`CBStockMap` 的任务轴位置转换也不应伪装成普通字段读取。

现有 `datasets.py` 的迁移关系保持直接：`_wide -> sql_panel`、
`_minute -> parquet_bars`、`_fundamental -> ranked_sql_panel`、
`_index_component -> sparse_sql_panel`、`_cb_stock_map -> static_relation`；
`_adjust_factor` 和 `_untradable` 只改为具名注册。`load_group()` 删除按 source 名称的
分支，只按 `DatasetSpec.reader` 查表调用。

Fundamental 的 ItemCode 动态发现属于 Catalog 扩展，不属于 Reader。首版保留一个具名的
`fundamental_items` Catalog expander；不为这一例外设计任意 discovery query DSL。

## 5. Catalog 配置

Dataset 配置新增稳定的 `reader` 字段，`source` 不再承担 Reader 分派职责：

```json
{
  "dataset_id": "stk_return_daily",
  "reader": "sql_panel",
  "asset": "stk",
  "freq": "1d",
  "table": "SmartQuant.ReturnDaily",
  "date_col": "DataDate",
  "code_col": "InnerCode",
  "trading_flag_col": "IfTradingDay"
}
```

Source 配置只描述逻辑字段落点和值语义：

```text
logical key
dataset_id
field / constant
ValueKind
semantic params
missing/default policy
```

Reader 选择只存在于 `DatasetSpec.reader`。SourceSpec 通过 `dataset_id` 引用
DatasetSpec，不复制 reader 名称；reader 也不进入 SourceRef、Term 或 LogicalPlan 的
语义身份。

## 6. 唯一 Load 规范化边界

`LoadNormalizer` 是 RawBatch 进入 Runtime 前唯一拥有以下职责的组件：

- 按 ReadDomain 把日期、资产和 step 坐标换算为稳定位置；
- 拒绝重复、越界或无法解析的坐标；
- 为每个 binding 分配或复用最终 `T × N × S` 数组；
- 转换为 `float64`，数据库 NULL 和缺失记录统一为 `NaN`；
- 将正负 Infinity 统一转换为 `NaN`；
- 根据 ValueKind 校验 MASK 的 `0/1/NaN` 和 CODE 的整数/NaN；
- 应用显式的 per-source 默认值；常量列由 Reader 为实际存在的物理行投影到 RawBatch；
- 返回只读的 `NormalizedSourceBatch`，并保证每个 binding 恰好一个数组。

### 6.1 固定处理顺序

LoadNormalizer 必须使用以下顺序，Reader 不得提前重复这些步骤：

1. 校验 bindings 属于同一 LoadGroup、共享兼容 ReadDomain，并确定全部 term_id；
2. 规范并校验各 Source 的 default 标量，按 default 或 NaN 分配最终数组，同时建立
   跨 batch occupancy 状态；
3. 对每个 RawBatch 校验 `coordinate_mode`、坐标键、term_id 键集合和所有列长度；
4. 将 `labels/flat/static` 坐标解析为最终位置，拒绝未知、越界或不适用的坐标模式；
5. 拒绝 batch 内以及不同 batch 之间的重复 date/asset/step 位置；
6. 把原始值转换为 `float64`，非空但无法数值化的值明确失败；
7. 将正负 Infinity 转换为 NaN；
8. 根据 ValueKind 校验 MASK 的 `0/1/NaN` 和 CODE 的整数/NaN；
9. 把值散布到预分配数组，未出现的位置保留已声明的 default；
10. finalize 时校验 term_id、shape 和 dtype，设置只读后一次性交付
    NormalizedSourceBatch。

任一步失败都丢弃该 LoadGroup 的全部未完成数组，不向 Runtime 返回部分结果。流式 Reader
仍使用同一个 normalizer/occupancy 状态，不能按 batch 各自 finalize。

Reader 不重复这些检查；Runtime 收到 NormalizedSourceBatch 后只检查 term_id 集合，
不再重新扫描 dtype、shape、Infinity 或 ValueKind。

分钟 Reader 可以持续返回 RawBatch，由同一个 LoadNormalizer 预分配并增量散布，继续
保持当前 Arrow streaming 的内存优势。静态关系由 normalizer 沿 ReadDomain 日期维广播，
Reader 不展开 `T × N` 原始行。

## 7. ValueKind 与 Infinity

ValueKind 只定义有限值的逻辑集合：NUMERIC、MASK、CODE。它不自动转换数组，也不单独
决定 Infinity 是错误还是 Missing。统一策略属于引擎数组值协议：Source Load 边界先把
正负 Infinity 转为 NaN，再执行 ValueKind 校验。

进入 Workspace 后不再为每个 OperatorTerm 做全数组 Infinity 扫描。内置 operator kernel
必须遵守“不得返回 Infinity”的契约；除零、非法对数等在 kernel 内直接生成 NaN。最终
输出边界保留一次兜底规范化。自定义 operator 的注册者承担相同契约，不满足时属于
operator 实现错误。

## 8. 验收标准

- ReturnDaily、CBReturnDaily 和 IndexQuote 共用 `sql_panel`，不再按 source 名称分派；
- 股票/转债分钟数据共用 `parquet_bars`；
- 同一 reader_kind 的新数据集可以只通过配置接入；
- Reader 测试只断言查询、字段和 RawBatch，Normalizer 测试集中断言坐标与值协议；
- 所有 Reader 都只返回 `labels/flat/static` 三种模式之一的同一 RawBatch；
- LoadNormalizer 的处理顺序、跨 batch 重复检测和失败原子性由集中测试锁定；
- Source 数组只在 LoadNormalizer 中做一次完整 shape/dtype/ValueKind 校验；
- Runtime 不再重复扫描已规范化 Source 数组；
- whole-domain、chunked 和多字段 LoadGroup 的结果与当前实现一致。
