# ADR 0009：编译前解析固定任务域

- 状态：已接受
- 日期：2026-07-24

> 补充说明（2026-07-27）：ADR 0016 已进一步决定，`ComputeResult` 直接携带任务实际使用的完整 `ResolvedExecutionDomain`，而不是只依赖原始 `DomainSpec` 或外部 domain ID 解释内存数组。

## 背景

计算结果是无标签的数组，而公式编译、叶子对齐、截面运算、结果解释和落盘都需要确定每个位置对应的日期、资产和日内 step。调用方通常只希望声明日期区间、日历和资产轴引用，不适合直接构造全部坐标。

每日变化的上市状态、指数成分和可交易性如果通过改变资产轴表达，会产生不规则数据结构并破坏统一的数组空间。

## 决策

一个 ComputeRequest 在任务期间使用固定、有序的日期轴、资产轴和 step 轴。

```text
DomainSpec
  -> DomainResolver
  -> ResolvedExecutionDomain
  -> Compiler
```

- DomainSpec 是调用方的简洁声明。
- ResolvedExecutionDomain 是编译和执行共同使用的不可变坐标契约。
- 资产轴是研究区间内固定的一组有序 InnerCode。
- 上市状态、指数成分、可交易性等每日变化条件通过特征或 mask 表达。
- ExecutionDomain 描述输出轴；输入读取区间仍由 PhysicalPlanner 根据计划推导。

## 解析边界

- 内联日期或资产代码的格式校验、去重和规范化可以由 DomainResolver 本地完成。
- 交易日历、命名资产轴和频率规格通过 DomainCatalog 或 SchemaCatalog 等外部元数据端口解析。
- DataProvider 不负责解析任务域；它在计划生成后读取公式叶子的实际数值。
- Engine Facade 协调 DomainResolver，并把解析结果传给 Compiler。

## 轴身份

架构要求轴身份明确、稳定且可比较，但不强制调用方手工提供 hash。

可以使用：

- 请求内联的完整有序坐标；
- 不可变轴引用及版本；
- 解析后由引擎计算的内容 fingerprint/hash。

具体采用哪一种属于协议和存储实现。结果 lineage 至少要能够还原或引用实际使用的坐标。

## 影响

- 当前由 `FeatureStore.resolve_space()` 隐式提供的任务空间需要改为显式 ResolvedExecutionDomain。
- Compiler 不依赖 FeatureStore 来确定输出 shape。
- 输出数组必须与 ResolvedExecutionDomain 或其明确引用一起传递。
- 不同任务可以使用不同轴；“固定”只约束单个任务生命周期，不表示全系统只有一个永久轴。
