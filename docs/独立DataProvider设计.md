# 独立 DataProvider 设计

- 状态：现有 Provider 已实现；Reader Strategy 与独立 LoadNormalizer 改造待实现
- 范围：新 batch pipeline 的正式数据提供者，包含真正的批量 `load_many()`
- 细化设计：[`Reader与Load规范化边界设计.md`](Reader与Load规范化边界设计.md)

## 1. 定位与分层

正式 Provider 原生实现：

```text
calendar_dates()
asset_codes()
describe_many()
bind_many()
load_many()
```

它服务于现有调用链：

```text
Formula AST -> Compiler -> LogicalPlan -> PhysicalPlanner
            -> Runtime -> DataProvider
```

Provider 负责协调 Source Catalog、任务坐标快照、物理绑定、Reader、LoadNormalizer、
任务级缓存和物理读取诊断。Reader 只执行物理 I/O，LoadNormalizer 独立负责把原始
记录转换为权威 Runtime 数组。Provider 不解析公式，不执行因子
operator，不做频率聚合、资产投影、日期分区或结果存储。

物理表、字段、路径和加载组只存在于 `bind_many()` 之后，不进入
`LogicalPlan` 或其 semantic identity。

## 2. 任务级生命周期

Provider 是任务级对象。Provider 创建时解析 Catalog；Compiler 完成公式 horizon
预分析后，将任务统一 read horizon 和 selector 传给 Provider 冻结 calendar 与资产轴。
同一任务的 Compiler、binding、Reader 和 Runtime 共享并冻结以下状态：

- Resolved Source Catalog；
- calendar 日期轴；
- 各资产类型的有序 master axis；
- Catalog、calendar 和资产轴 fingerprint；
- 任务级缓存、连接资源和 diagnostics。

任务自然结束、异常或 ResultStream 提前关闭时均应释放资源。不同任务重新解析
Catalog 和坐标快照，不复用位置型资产映射。

## 3. Calendar 与资产主轴

- calendar 从 `SmartQuant.JY_TradingDayNew` 读取，使用 `TradingDate`，并固定过滤
  `SecuMarket = 83 AND IfTradingDay = 1`；
- 当前数据库没有唯一权威的证券信息主表，股票、转债和指数资产轴从对应行情表
  的稳定代码并集解析；
- 任务资产轴的查询范围是完整输出 Domain 加本批公式所需的任务级 lookback，即
  `axis_dates = output_dates + 之前最多 job_lookback 个交易 session`；
- 同一任务的 whole-domain 和所有日期分区共享一次解析并冻结的任务资产轴，禁止按
  partition 分别查询代码并集；
- `asset_scope="all"` 只使用上述任务 read horizon 内出现过的 `InnerCode` 并集；
- 完整代码并集去重后按明确的稳定字段排序；
- 显式资产子集只在上述任务 read horizon 中查询并验证调用方给出的代码，保留调用
  方顺序，不需要先读取全部 universe；
- 指数应优先使用显式 universe，例如只传入任务需要的几个指数，而不是默认读取
  全部指数；指数 universe 直接使用 `InnerCode`；
- 上市、退市、停牌和交易状态不进一步裁剪已经解析的任务轴，而通过数据缺失或
  显式 mask 表达。

资产来源为股票 `SmartQuant.ReturnDaily`、转债 `SmartQuant.CBReturnDaily` 和指数
`JYDB.QT_IndexQuote`。三类任务轴统一使用 `InnerCode`；股票和转债对外部
`SecuCode` 的读取按日期映射到该内部代码轴。

### 3.1 lookback 与资产轴解析顺序

当前 Compiler 在正式 Term lowering 后才得到 `job_lookback`，而 Term lowering 又依赖
已冻结资产轴。任务区间资产轴因此要求增加一个不产生 LogicalPlan 的 horizon
预分析阶段：

```text
符号绑定 / helper 展开
  -> 规范化 operator 调用参数
  -> AST horizon 预分析，得到 job past horizon
  -> calendar 上解析 output dates 和 axis/read dates
  -> Provider 按 axis/read dates 冻结任务资产轴
  -> 正式 Domain lowering / Term lowering / CSE
  -> 校验最终 LogicalPlan.job_lookback 与预分析一致
```

horizon 参数规范化和 lookback 规则必须与正式 operator lowering 复用同一实现，不能
维护两套窗口语义。未来支持 future horizon 时，资产轴查询范围相应扩展到经过授权的
future read dates。

## 4. Source Catalog

[`data_sources.json`](../src/factor_engine/data_provider/data_sources.json) 作为版本化的
Catalog 构建规则。每个任务启动时重新扫描配置的数据集字段和基本面目录，生成
只存在于任务内存中的 Catalog；暂不持久化解析结果，但记录其
fingerprint 用于诊断和复现。

配置保存物理位置、`reader` 策略和单数据集差异：Source 表和路径、资产轴使用的行情表
和日期列，以及 `source_tables[].exclude_fields` 这类单表例外。稳定字段的 `ValueKind`
和通用排除规则位于 [`data_provider/catalog.py`](../src/factor_engine/data_provider/catalog.py)，
Reader 注册与少数具名派生查询位于 [`data_provider/datasets.py`](../src/factor_engine/data_provider/datasets.py)，
共用 SQL/DuckDB 能力位于
[`data_provider/backend.py`](../src/factor_engine/data_provider/backend.py)，不把每个固定列
重复展开成配置协议。`SmartQuantDataProvider` 只协调任务状态和五个 Provider 方法。

正式实现内部按职责分为 `catalog.py`、`backend.py`、`datasets.py`、`normalize.py`
和薄 `smartquant.py`。旧 `legacy/data/router.py`、`legacy/data/smartquant.py` 与 FeatureArray
只属于 legacy 研究层，不进入正式 Provider 依赖链。

Catalog 对每个 Dataset 和逻辑 Source 至少描述：

```text
logical key
asset type / frequency / step rule / calendar
ValueKind
允许的 semantic params 及其校验规则
physical source / table / field / query params
batch compatibility rule
reader type
```

语义描述用于 `describe_many()`；表、字段和查询参数只在 `bind_many()` 阶段形成
物理 `SourceSpec`。物理位置变化不得改变等价 LogicalPlan 的身份。

`SourceSpec` 继续保持当前职责和字段范围。数据集级批量请求由 Provider 内部临时
组装，不扩展成新的公共计划对象。`cb.1d.underlying_stk_col` 只在 Catalog
中声明原生 `cb` 轴、`CODE` 值类型和 `kind="col"`；不增加
`reference_asset` 字段。引用的股票轴是 `_cb_stock_map()` 的固定业务规则。

## 5. 首期数据范围

首期以当前 `data_sources.json` 和现有基本面目录为范围：

- 股票、转债和指数日频宽表字段；
- 股票 1min/5min 和转债 1min parquet；
- 基本面多季度数据；
- 复权因子、不可交易 mask；
- 行业 code、指数成分权重和成员 mask；
- 转债与正股的原始关系数据。

Provider 不执行分钟频率 resample。已保存因子继续通过组合 Provider 接入，不纳入
基础 Provider 与正式 FactorRepository 的职责。

基本面首版只发布同时满足以下条件的条目：

- 存在于 `SmartQuant.Fundamental_ItemCode`；
- 对应的 `SmartQuant.Fundamental_Item{ItemCode}` 物理表实际存在。

当前数据库满足条件的条目为 84 个。其余 Catalog 条目不回退到 JYDB 原始财报宽表，
并在 `describe_many()` 阶段明确报告不支持，避免 Provider 猜测 PIT、报表版本和口径
选择语义。同一 ItemCode、相同 PIT/quarters 查询语义下的多个值列可以批量读取。

分钟 parquet 使用 `data_sources.json` 中配置的公司 HPC 路径。开发机未挂载这些路径
不代表 Catalog 无效；单元测试使用临时 parquet，真实路径和性能集成测试在 HPC
环境执行。

## 6. Reader Strategy 与真正的批量读取

相同物理数据集、相同 ReadDomain 和兼容查询语义的字段组成一个 LoadGroup：

- 日频宽表使用一次 SQL 读取多个字段；
- 分钟 parquet 使用一次 scan 和列裁剪读取多个字段；
- 日期过滤、代码映射、step 对齐和连接资源在组内共享；
- 不同表、不同 ReadDomain 或不兼容 row filter 不得合组；
- 无法批量读取的 Source 允许显式 fallback，并记录原因；
- 组内任一必要字段失败时，整个 LoadGroup 和任务立即失败。

Reader 按物理布局而不是业务 frequency 分类。首版通用策略为 `sql_panel`、
`parquet_bars`、`ranked_sql_panel`、`sparse_sql_panel` 和 `static_relation`；
AdjustFactor、Untradable 使用各自具名 Reader，不提供万能 `custom` 分派。现有具体
source 名称不再直接作为通用 loader 分派键；详细映射和配置边界见
[`Reader与Load规范化边界设计.md`](Reader与Load规范化边界设计.md)。

## 7. 数据规范化

Reader 返回 RawBatch；独立 LoadNormalizer 将其转换为 NormalizedSourceBatch。
`load_many()` 对外返回的每个数组必须精确满足：

```text
dtype   = float64
shape   = T x N x S
missing = NaN
```

LoadNormalizer 是唯一负责按 binding 对齐日期、资产代码顺序和原生 step，规范化数据库
NULL、sentinel、bool、code 和数值字段，并校验 MASK/CODE 协议、重复坐标、未知坐标、
错误 dtype、shape 和不完整 LoadGroup 的组件。Reader 和 Runtime 不重复这些转换与扫描。

ValueKind 只定义有限值的逻辑集合；Infinity 策略属于统一 Runtime 值协议。LoadNormalizer
先把正负 Infinity 转为 NaN，再执行 MASK/CODE 校验。

查询成功但某资产某日没有观测属于正常 Missing，填充 NaN；文件、字段或查询失败，
无法解释的重复坐标以及 Provider 契约错误均中止任务。

## 8. 缓存与诊断

第一版只使用任务级缓存：

- Catalog、calendar、资产轴、代码映射和小型静态关系可缓存到任务结束；
- 日行情、分钟行情和分区 `FeatureArray` 等大数组默认不做长期缓存；
- 后续如需缓存分区数据，必须使用明确容量上限的 LRU。

Provider 记录任务级物理读取事件，包括数据集、字段、分区范围、批量或 fallback、
cache hit、物理查询/scan 次数、行数或字节数、耗时和错误 Source。该诊断用于验证
一次 `load_many()` 是否真正合并为一次物理 I/O，并最终并入执行统计。

## 9. 股票到转债的任务级 mapping

关系 Source `cb.1d.underlying_stk_col` 的原生轴是 `cb`。Provider 按当前
转债 ReadDomain 读取原始正股 InnerCode，`_cb_stock_map()` 内固定使用当前任务
实例的 `axes["stk"]` 生成列位置。该规则不抽象为可配置引用资产类型。
正股不在任务股票轴时，列位置为 NaN。

Provider 只产生任务内的位置数据；股票到转债的 gather 仍由通用
`lookup_by_col()` operator 执行。Provider 实例不得跨任务切换资产轴，位置映射也
不得进入跨任务缓存。

## 10. 物理表接入流程

具体物理表采用逐类确认方式：

1. 需求方指定业务数据、候选表和预期语义；
2. 只读检查表结构、字段类型、坐标列、主键/重复行、日期覆盖和代码覆盖；
3. 确认逻辑 key、`InputSpec`、ValueKind、物理字段、过滤条件及批量兼容规则；
4. 需求方确认映射；
5. 将规则加入 Catalog，并实现对应批量 Reader 与契约测试。

任何无法仅凭表结构确定的业务口径不得由 Provider 自动猜测。
