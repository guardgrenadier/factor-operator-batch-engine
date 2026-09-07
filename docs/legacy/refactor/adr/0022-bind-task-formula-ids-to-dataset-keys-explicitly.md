# ADR 0022：显式绑定任务公式 ID 与长期数据集 Key

- 状态：已接受
- 日期：2026-07-27

## 背景

`FormulaItem.id` 用于在一次 Compute job 内关联公式、OutputChunk、结果和失败，只要求任务内唯一。自动挖掘可以使用 `candidate_0042` 等临时候选 ID。

长期因子数据集需要跨任务稳定的身份。现有系统使用 `asset.freq.name`，公司因子目录也可能维护 `runner_value_xxxxxx` 等登记 ID。把这些命名规则强制加入 formula ID，会使计算协议依赖研究注册和公司存储体系。

## 决策

任务内 `formula_id` 与长期 `dataset_key` 保持不同概念。

启用磁盘输出时，磁盘输出配置为任务中的每个 formula ID 提供显式目标绑定：

```text
DiskOutputSpec
├── store
└── targets
    └── formula_id -> DatasetKey
```

- `formula_id` 继续只负责本次任务内关联；
- `DatasetKey` 是 ArtifactStore 中长期稳定的数据集身份；
- DatasetKey 可以来自 `asset.freq.name`、公司因子登记 ID 或上层生成的命名空间；
- ArtifactStore 负责把 DatasetKey 解析到实际存储位置，调用方不直接拼接文件路径；
- 映射由研究适配层、批任务服务或公司因子目录提供，不进入 Compiler 或 Runtime。

`ComputeRequest` 仍只描述“算什么”，不携带物化路径或公司因子目录状态。磁盘目标绑定属于独立的输出控制面。

## 影响

- 同一个公式任务可以在不同环境写入不同 ArtifactStore 或 DatasetKey，而不改变计算语义。
- 研究场景可以令 formula ID 与 DatasetKey 相同，但协议不要求它们相同。
- 自动挖掘的内存任务不需要为临时候选分配长期 DatasetKey。
- `ComputeResult.artifacts` 仍以 formula ID 为 key；ArtifactRef 中记录实际 DatasetKey。
- `OutputOptions.disk=true` 时，targets 必须覆盖任务中所有 formula ID，因为 ADR 0019 已决定磁盘目的地对整批统一。

## 本 ADR 不决定

- DatasetKey 的具体字符串格式和命名空间规则；
- 公司因子目录与 ArtifactStore catalog 的同步方式；
- 同一 DatasetKey 被并发任务更新时的冲突策略；
- append、overwrite 或区间重算的写入模式协议。
