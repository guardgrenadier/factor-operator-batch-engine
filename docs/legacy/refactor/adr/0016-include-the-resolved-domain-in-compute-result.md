# ADR 0016：ComputeResult 直接携带完整解析执行域

- 状态：已接受
- 日期：2026-07-27

## 背景

ADR 0015 决定启用内存结果时，`ComputeResult.arrays[formula_id]` 是与完整 `ResolvedExecutionDomain` 对齐的裸 `ndarray`。

`ndarray` 自身不携带日期、资产和 step 坐标。调用方提交的 `DomainSpec` 只是简洁声明，交易日历、命名资产轴等内容可能要经过 `DomainResolver` 才能得到任务实际使用的精确有序轴。因此，仅返回数组或原始 `DomainSpec` 不足以无歧义地解释结果。

## 决策

`ComputeResult` 直接携带本次任务实际使用的完整 `ResolvedExecutionDomain`：

```text
ComputeResult
├── domain: ResolvedExecutionDomain
├── arrays: formula_id -> ndarray
├── artifacts
├── failures
└── job_report
```

一个任务只有一个同质计算域，因此所有内存结果数组共享顶层的 `domain`，不在每个数组上重复保存轴。

`ComputeResult.domain` 是已经解析完成的精确坐标契约，不是原始 `DomainSpec`，也不只是需要调用方再次查询才能解释的 domain ID。

## 影响

- `ComputeResult` 在内存 API 中可以独立、无歧义地解释每个结果数组的位置。
- 调用方不需要重新调用 `DomainResolver`，避免命名轴或交易日历后来变化导致结果解释漂移。
- `arrays[formula_id].shape` 必须与 `domain` 的完整日期轴、资产轴和 step 轴一致。
- 轴对象可以在 `ComputeResult` 内共享，不随每个公式重复复制。

## 本 ADR 不决定

- `ResolvedExecutionDomain` 的具体 Python 序列化形式；
- 跨进程或远程 API 是否另外提供紧凑 wire representation；
- ArtifactStore 如何在磁盘产物中编码、去重或引用同一执行域；
- 失败公式的部分数组是否进入最终 `arrays`。
