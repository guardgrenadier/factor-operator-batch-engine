# ADR 0025：每日分区只保存非全 missing 的资产行

- 状态：已接受
- 日期：2026-07-27

## 背景

Runtime 输出与完整 `ResolvedExecutionDomain.asset_axis` 对齐。对于某一天，部分资产的所有 step 可能都为 missing。

每日分区可以保存完整任务资产轴，也可以像当前 FeatureStore 一样，只保存至少存在一个非 missing step 的资产行，并通过并列的 codes 数组保留坐标。

保存完整轴可以还原“资产在原任务轴中但结果全 missing”与“资产不在原任务轴中”的区别，但当前业务不需要这一区分。两者在加载到目标域后都表现为 missing。

## 决策

每日分区只保存至少一个 step 非 missing 的资产行：

```text
keep[row] = any(not_missing(values[row, :]))
```

逻辑布局为：

```text
codes.shape  = (K,)
values.shape = (K, S)
```

其中：

- `K` 是当天被保留的资产行数，可以小于任务完整资产数；
- `S` 必须与因子数据集的 step 规格一致；
- `codes[i]` 与 `values[i, :]` 一一对应；
- codes 保持确定的有序顺序；
- load 时先创建目标域的全 missing 数组，再按 codes 对齐填入。

首版 Runtime 使用 float64 + NaN，因此“全 missing”按一行所有 step 都为 NaN 判断。mask 的全 `0` 行和 code 的有效零值行不会被删除。

## 影响

- 保留现有每日稀疏存储的空间优势。
- 资产轴演化不会要求重写历史日文件。
- ArtifactStore 无法从日分区还原原计算任务中包含但结果全 missing 的资产；这是已接受的信息丢失。
- 如果未来需要记录原任务覆盖范围，应通过独立 lineage/coverage metadata 表达，而不是扩大全部日数组。

## 本 ADR 不决定

- 首版不为“整个日期全部 missing”设计额外完成标记或特殊协议，该场景不作为当前设计需求；
- codes 的具体 integer dtype；
- 同一日期出现重复 code 时的校验策略。
