# 因子计算领域语言

本文定义任务级因子公式编译与计算系统的统一领域语言。这里只记录概念含义，不记录实现或架构决策。

## 公式语言

**Formula（公式）**:
一组自包含的顺序命名表达式，最后一个绑定产生一个因子输出。
_避免使用_：脚本、特征定义

**Factor（因子）**:
计算任务中由 formula ID 标识、并与该任务输出域对齐的公式结果。
_避免使用_：Feature、信号数组

**Formula Batch（公式批次）**:
共享公共输入和一个输出域的一组相互独立公式。
_避免使用_：任务脚本、公式列表

**Common Input（公共输入，API 名称 `common_inputs`）**:
公式批次内所有公式都可以引用的命名表达式。
_避免使用_：全局变量、已注册特征

**Local Binding（局部绑定）**:
只在同一公式后续表达式中可见的命名表达式。
_避免使用_：中间特征、临时因子

**Symbol Reference（符号引用）**:
对当前作用域内已经绑定的公共输入或局部绑定的引用。
_避免使用_：Alias、Feature Reference

**Helper**:
在规范化之前展开为 source 引用和 operator 表达式的纯公式语言便利结构。
_避免使用_：Loader、source 注册

## 数据输入

**Source Reference（数据源引用）**:
公式对所需外部逻辑数据的声明，与数据的物理位置无关。
_避免使用_：source 路径、表引用

**Input Specification（输入规格）**:
供编译使用的 source 语义契约，描述其资产类型、频率、step 结构和 ValueKind。
_避免使用_：source 配置、物理 schema

**Source Specification（物理源规格）**:
描述外部数据位于何处以及如何读取的物理信息。
_避免使用_：Input Specification、公式依赖

**Source Binding（数据源绑定）**:
特定任务中 SourceTerm、SourceSpec 与具体 ReadDomain 之间的关联。
_避免使用_：source 注册、全局路由

**Load Group（加载组）**:
能够以兼容查询语义从同一物理数据集中共同读取的一组 SourceBinding。
_避免使用_：source 缓存、依赖组

**Reader**:
按 DatasetSpec 声明的具名物理读取器，执行读取并流式返回 RawBatch，不执行最终数组协议。
_避免使用_：Loader、数据集分派函数

**SQL Query Builder（SQL 查询构造器）**:
只生成受约束规范 labels SQL 的具名函数，不执行查询、不散布数组。
_避免使用_：SQL 模板、查询 DSL

**RawBatch**:
Reader 产出的原始批次，携带 labels/flat/static 坐标列或位置提示与原始值列。
_避免使用_：中间表、结果集

**LoadNormalizer（加载规范化器）**:
Source 数组进入 Runtime 前的唯一权威规范化边界，独占坐标校验、散布、dtype、缺失、ValueKind 与默认值。
_避免使用_：Normalizer 散落各处、Runtime 重复校验

## Domain

**Asset Scope（任务资产范围）**:
一个计算任务允许使用的资产类型和有序资产选择。
_避免使用_：不涉及成员语义时称为 Universe、资产列表

**Output Domain（输出域）**:
计算任务请求返回的精确日期、目标资产、频率和 step 坐标。
_避免使用_：读取范围、source 空间

**Term Domain**:
SourceTerm 值的资产、频率、step 和日历身份，供绑定与读取域计算使用。
_避免使用_：数组 shape、Output Domain

**ArrayLayout（数组布局）**:
普通算子值的物理形状（N、S）及不参与兼容性检查的溯源提示，是 OperatorTerm 携带的唯一布局信息。
_避免使用_：Term Domain（算子值）、业务坐标身份

**Read Domain（读取域）**:
一个物理分区实际读取的日期和坐标范围，包含 lookback 所需历史。
_避免使用_：输出范围、Execution Domain

**Lookback（历史回看）**:
在不改变请求输出日期的前提下，计算结果所需的有限前置交易 session 数。
_避免使用_：Offset、预热期

## 执行

**Term**:
任务计算图中的规范化可执行节点，只可能是 LiteralTerm、SourceTerm 或 OperatorTerm。
_避免使用_：AST 节点、Feature

**Logical Plan（逻辑计划）**:
包含 Term 依赖、Domain、值契约和 lookback 的多输出逻辑图。
_避免使用_：执行调度、Physical Plan

**Physical Plan（物理计划）**:
某次 Logical Plan 执行所使用的有序分区、ReadDomain 和 SourceBinding。
_避免使用_：公式语义、Logical Plan

**Workspace**:
只在当前分区内保存、并仅在仍有消费者时保留的 Term 值集合。
_避免使用_：结果仓库、跨任务缓存

## 结果

**Result Chunk（结果分块）**:
一个公式在共享 Output Domain 某个切片上的结果值。
_避免使用_：部分因子、已保存因子

**Result Stream（结果流）**:
单次消费的有序 provisional ResultChunk 序列；只有完整无错地消费后才表示成功。
_避免使用_：结果回调、已提交数据集

**Compute Result（完整计算结果）**:
完整内存公式数组及其共同 ResolvedOutputDomain。
_避免使用_：Workspace、ResultChunk 列表

**Stored Factor（已保存因子）**:
已经持久化、并在后续公式中作为外部逻辑 source 引用的因子。
_避免使用_：已注册公式、Runtime 值

## 不推荐的模糊术语

**Feature**:
该词可能指 source 字段、公式定义、中间值、因子输出或已保存数据集，不应无修饰地使用；应改用上面更精确的术语。
