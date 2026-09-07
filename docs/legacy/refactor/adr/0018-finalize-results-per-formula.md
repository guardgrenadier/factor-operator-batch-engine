# ADR 0018：按公式独立 finalize 结果

- 状态：已接受
- 日期：2026-07-27

## 背景

ADR 0017 已决定以 `formula_id` 为结果完整性和清理单位：公式任一必要分区失败时，清理该公式的全部暂存结果，但不影响独立公式。

成功结果还需要确定提交时点。可以等待整个任务结束统一 finalize，也可以在单个公式的所有必要分区完成后立即 finalize。

如果等待任务结束，一个无关公式的失败或长时间运行会延迟已经完整成功的公式发布，并延长其磁盘暂存生命周期。这与按公式隔离失败的边界不一致。

## 决策

结果按 `formula_id` 独立 finalize。

当且仅当某个公式的全部必要分区成功完成后，Scheduler 才将该公式标记为完整，并立即要求已启用的结果组件 finalize 该公式，不等待其他公式完成。

- 对内存结果，finalize 表示完整 ndarray 已封闭，不能再写入，并成为最终 `ComputeResult.arrays` 的候选；数组继续保留到任务结束返回。
- 对磁盘结果，每个 OutputChunk 就绪后立即写入该公式的工作路径，不等待其他分区。finalize 只表示该路径中的全部必要分块已经成功，可写入完成标记、metadata、注册信息或执行等价的发布动作，并产生稳定的 ArtifactRef；finalize 不重新写入此前分块。
- 某个独立公式后来失败，不回滚已经 finalize 的其他公式。

公式 finalize 前发生任何必要分区失败，执行 ADR 0017 的公式级 abort 和清理。

磁盘生命周期因此是：

```text
begin(formula_id)
  -> write_chunk(OutputChunk)*
  -> finalize(formula_id)   # 全部分区成功
     或
  -> abort(formula_id)      # 任一必要分区失败，删除整个工作路径
```

## 影响

- Scheduler 必须知道每个 formula ID 的必要输出分区集合，并跟踪其成功、失败和完成状态。
- MemoryResultAssembler 和磁盘结果组件都需要公式级 `finalize(formula_id)` 与 `abort(formula_id)` 或等价能力。
- 结果组件不需要实现整批公式的共同事务。
- 磁盘分块在计算过程中持续写入；完整 artifact 可能在整个任务返回 `ComputeResult` 之前已经正式可用。
- `ComputeResult` 仍在任务结束时统一汇总已经 finalize 的数组引用、ArtifactRef、失败和 JobReport。

## 本 ADR 不决定

- 任务级基础设施故障发生时，是否保留此前已经 finalize 的公式；
- 进程崩溃后如何恢复 finalize 状态或清理遗留暂存数据；
- ArtifactStore 的文件布局和原子发布机制；
- `OutputOptions` 按整批还是按 formula ID 选择内存与磁盘目的地。
