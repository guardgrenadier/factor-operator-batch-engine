# ADR 0021：长期因子数据集使用自带坐标的每日分区

- 状态：已接受
- 日期：2026-07-27

## 背景

当前 FeatureStore 根路径绑定一个 Snapshot，包括固定研究日期区间和固定资产轴。因子 metadata 也按该 Snapshot 校验完整 dates/codes hash。

这种模型适合一次研究快照，但不适合让同一个因子跨多个计算任务持续追加或覆盖日期。不同任务的研究区间和资产池可能变化；如果存储路径绑定一个固定 AssetSpace，就需要为新的区间或资产轴重建 Store。

现有按日保存 `data` 和 `code` 的设计仍有独立价值：它天然对应每日增量，并让每一天的数据携带类似 DataFrame index 的资产坐标。

## 决策

ArtifactStore 管理可跨任务增量更新的长期因子数据集。

ArtifactStore 根路径不绑定研究区间或固定资产轴。每个因子数据集只绑定：

- 稳定的 dataset key；
- 因子逻辑身份及必要 lineage；
- asset type；
- frequency 和 step 规格；
- ValueKind、物理 dtype、missing 等值规格；
- 存储 schema/version。

因子数据集不绑定：

- 固定开始和结束日期；
- 一次研究使用的完整日期轴；
- 永久固定的资产成员轴。

物理存储以日期为逻辑分区。每个日期分区保存自己的有序 code 轴和与之对齐的数据数组：

```text
factor dataset
├── metadata
└── day partitions
    ├── YYYYMMDD
    │   ├── codes
    │   └── values
    └── ...
```

不同日期、不同增量任务写入的 code 集合可以不同。读取时，ArtifactStore 根据每日 codes 把数据重新对齐到调用方请求的 `ResolvedExecutionDomain.asset_axis`；不存在的目标坐标填充 missing。

## 任务与存储空间边界

- 一次 Compute job 使用自己的完整 `ResolvedExecutionDomain`。
- 每个 OutputChunk 携带该域中的明确 output slice。
- ArtifactStore 按 OutputChunk 的日期和坐标写入每日分区，但不把该任务的完整空间变成 Store 的永久空间。
- lookback 所需的 read dates 属于 PhysicalPlan 和数据绑定，不进入因子存储空间定义。

## 写入生命周期

长期正式数据集与本次任务的工作路径分开：

```text
published dataset
  datasets/{dataset_key}/...

current write session
  staging/{job_id}/{formula_id}/...
```

- OutputChunk 就绪后立即写入本次工作路径；
- 公式成功后，把本次日期分区提交到长期数据集；
- 公式失败时，删除本次工作路径；
- 失败不能删除或破坏长期数据集中此前成功发布的历史分区。

具体目录名只用于说明生命周期，不是最终文件布局承诺。

## 影响

- 同一因子路径可以跨任务追加新日期或重算覆盖已有日期。
- 资产轴演化不要求迁移整份历史矩阵。
- ArtifactStore load API 必须接收目标 ResolvedExecutionDomain 或等价的精确轴请求，并完成按日 code 对齐。
- 数据集级 metadata 不能继续记录一个永久的完整 dates hash 或 codes hash 作为读取前提。
- ADR 0026 已进一步决定首版继续使用按日 `code.npy + data.npy` 编码。

## 本 ADR 不决定

- 是否在未来为日内大数组增加其他编码或更细分区；
- 每日 codes 保存全轴还是仅保存有效资产；
- 并发写入同一日期时的冲突策略；
- dataset key 的命名、命名空间和 formula ID 绑定协议；
- catalog、manifest 和原子发布的具体结构。
