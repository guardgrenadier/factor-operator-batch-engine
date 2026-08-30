# ADR 0019：整批统一选择内存与磁盘输出

- 状态：已接受
- 日期：2026-07-27

## 背景

一次 ComputeRequest 包含同一 ResolvedExecutionDomain 上的一组公式。结果控制面需要决定启用内存装配、磁盘写入或两者同时启用。

如果按 formula ID 分别选择目的地，控制面、资源估算和结果完整性状态都会增加一层映射。当前三个场景不要求同一任务中的公式使用不同目的地。

## 决策

`OutputOptions` 对整批公式统一生效：

```text
OutputOptions
├── memory: bool
└── disk: bool
```

- `memory=true` 时，所有成功公式都装配完整内存数组；
- `disk=true` 时，所有成功公式都写入磁盘产物；
- 两者可以同时启用；
- 不在第一版提供按 formula ID 选择目的地的配置。

`OutputOptions` 描述结果去向，不改变公式语义，因此不进入 `ComputeRequest`。

## 影响

- 自动挖掘可以选择仅内存，高频研究可以选择仅磁盘，低频研究可以同时启用两者。
- 内存预算按整批公式的完整结果估算；需要降低峰值时，由上层减小公式批量。
- 如果未来出现同一共享 DAG 中不同公式确实需要不同目的地的场景，可以扩展 OutputOptions，而不改变 OutputChunk 的 formula ID 和 partition ID 协议。

## 本 ADR 不决定

- memory 与 disk 同时启用时，其中一个结果组件失败的处理方式；
- disk 目的地的路径、覆盖和 ArtifactStore 配置；
- 是否允许 memory=false 且 disk=false 的执行请求。
