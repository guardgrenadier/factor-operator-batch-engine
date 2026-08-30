# ADR 0026：校验数据集语义并直接 upsert 请求日期

- 状态：已接受
- 日期：2026-07-27

## 背景

长期因子数据集会被多个计算任务持续更新。同一个 DatasetKey 如果被不同公式或不兼容的数据规格写入，会形成历史日期与新增日期语义不一致的混合数据集。

另一方面，ADR 0024 已决定所有结果先写 staging，成功后再把本次涉及日期的文件替换到正式目录。该流程已经同时覆盖新增日期和重算已有日期，不需要再增加 append/overwrite 写入模式。

## 决策

### 数据集语义校验

因子数据集 metadata 保存稳定的 semantic signature 和数据规格。

每次开始 Artifact Write Session 时：

- DatasetKey 不存在：使用本次公式语义和数据规格创建新数据集；
- DatasetKey 已存在：校验本次 semantic signature 和数据规格与已有数据集兼容；
- 不兼容：在写正式数据前拒绝本次会话，禁止静默混写。

至少校验：

- 规范化公式语义签名；
- asset type；
- frequency 和 step 规格；
- ValueKind、physical dtype 和 missing 约定；
- 存储 schema version。

semantic signature 应基于规范化计算语义，而不是仅比较原始公式字符串。具体 hash 字段和版本编码在实现设计中确定。

公式定义发生不兼容变化时，首版使用新的 DatasetKey，或由独立管理操作显式重建数据集；普通增量写入不负责迁移公式定义。

### 日期 upsert

不提供 append、overwrite 或其他写入模式。

一次成功写入会话对其请求的全部日期执行统一 upsert：

- 正式数据中不存在该日期：新增日分区；
- 正式数据中已经存在该日期：用 staged code/data 文件替换旧文件；
- 未被本次请求覆盖的历史日期：保持不变。

首版继续采用按日 `code.npy + data.npy` 编码和 ADR 0024 的 staging 替换流程。

## 影响

- 每日增量和区间重算使用同一条写入路径。
- 调用方不需要选择容易误用的 overwrite flag。
- 同一 DatasetKey 不会静默混入不同公式或不兼容张量规格。
- DatasetKey 的定义变更不作为普通增量更新处理。

## 本 ADR 不决定

- semantic signature 的具体序列化和 hash 算法；
- 有意重建或删除数据集的管理 API；
- 并发任务同时更新同一 DatasetKey 的处理；
- finalize 中途崩溃后的修复工具。
