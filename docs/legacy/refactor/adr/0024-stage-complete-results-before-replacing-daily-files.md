# ADR 0024：完整写入 staging 后逐日替换正式文件

- 状态：已接受
- 日期：2026-07-27
- 取代：ADR 0023 的跨日期原子可见性要求

## 背景

ADR 0023 要求一次公式写入会话涉及的全部日期严格原子可见，并建议使用 manifest 切换或 generation 引用。

当前首版更优先保持 ArtifactStore 简单。实际必需的保证是：公式仍在计算或写 staging 时发生失败，不能破坏已经发布的历史文件。首版不要求在 finalize 替换多个正式文件的过程中仍具备进程崩溃恢复和并发读快照。

## 决策

继续使用直接、按日的正式文件布局，并为每次公式写入创建独立 staging：

```text
datasets/{dataset_key}/
├── metadata.json
├── data/
│   └── YYYYMMDD.npy
├── code/
│   └── YYYYMMDD.npy
└── staging/
    └── {session_id}/
        ├── data/
        └── code/
```

写入生命周期：

1. OutputChunk 就绪后立即写入本次 `staging/{session_id}`；
2. 任一计算分区或 staging 写入失败时，删除整个 session，正式文件保持不变；
3. 全部必要分区及 staging 写入成功后进入 finalize；
4. finalize 把本次涉及日期的 staged code/data 文件逐个替换到正式目录；
5. 文件替换完成后更新 dataset metadata，并删除 staging session。

不使用权威分区 manifest、不可变 generation object 或整个历史目录复制。

## 保证范围

首版保证：

- 计算失败不会改变正式数据；
- finalize 开始前的 staging 写入失败不会改变正式数据；
- 每个单独文件可以使用底层文件系统提供的原子替换能力；
- 失败 session 可以通过删除一个明确工作目录清理。

首版不保证：

- 多个日期文件之间的原子切换；
- 同一日期的 code 文件和 data 文件之间的共同原子切换；
- finalize 替换过程中进程崩溃或 I/O 故障后的自动回滚；
- 并发 reader 在 finalize 期间看到单一一致版本；
- 历史版本保留或 time travel。

因此该协议称为“完整 staging 后发布”，不再称为严格的“写入会话整体原子发布”。

## 影响

- ArtifactStore 不需要维护日期到 generation 的引用 manifest。
- 覆盖已有日期时，旧文件在对应 staged 文件被替换前保持可用，替换后不保留旧版本。
- metadata 应在本次日文件替换完成后更新，但 metadata 与所有日文件仍不是共同原子事务。
- 如果未来需要崩溃恢复、并发快照或严格多日期原子性，可以在不改变 OutputChunk 和 DiskResultWriter 生命周期的前提下增加 manifest/generation 后端。

## 本 ADR 不决定

- 每日 code/data 数组保存全资产轴还是只保存非全 missing 行；
- DatasetKey 并发写入的锁和冲突策略；
- metadata 的完整字段和 schema 版本；
- staging 孤儿目录的定期清理方式。
