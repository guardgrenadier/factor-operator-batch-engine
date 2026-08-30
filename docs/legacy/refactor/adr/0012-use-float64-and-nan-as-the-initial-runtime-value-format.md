# ADR 0012：第一版 Runtime 统一使用 float64 与 NaN

- 状态：已接受
- 日期：2026-07-24

## 背景

通用 nullable tensor 系统需要处理多种 NumPy dtype、validity bitmap、sentinel、类型提升和缺失传播，明显增加 Compiler、DataProvider、算子和 ResultSink 的复杂度。

当前因子计算主要处理连续数值、mask 和少量行业/映射 code，可以采用更小的值协议。

## 决策

第一版 Runtime 的所有计算张量统一使用：

```text
physical dtype = np.float64
missing        = np.nan
```

Compiler 只区分三种 ValueKind：

- `numeric`；
- `mask`；
- `code`。

mask 的编码为：

```text
1.0 = True
0.0 = False
NaN = Missing
```

code 使用有限、整数值的 float64，NaN 表示缺失。

## 层间职责

- SchemaCatalog 声明叶子的 ValueKind 和外部缺失约定。
- DataProvider 把 SQL NULL、bool、integer、sentinel 和其他源类型转换为 float64 + NaN。
- 比较算子在任一输入 missing 时返回 NaN，而不是 False。
- mask 算子使用三值逻辑；`mask_not(NaN)` 保持 NaN。
- 数值算子按契约把非法或非有限结果规范化为 NaN。
- ResultSink 根据 ValueKind 转换成目标系统的浮点、nullable bool 或 nullable integer。

## 暂缓能力

- validity bitmap；
- nullable bool/int 物理数组；
- 多 NumPy dtype 推导与提升；
- float32 保留；
- 通用字符串或 category tensor；
- 为 mask/code 单独设计压缩后端。

这些能力可以在不改变 DSL、Canonical IR 和逻辑 ValueKind 的前提下由未来 RuntimeBackend 扩展。

## 内存影响

mask 和 code 使用 float64，相比 bool/int 小类型会增加内存。原始 code 和许多业务 mask 通常是日频，初期可接受；日内公式产生的临时 mask 仍可能达到完整 `T x N x S` shape。

第一版执行侧必须：

- 在 PhysicalPlanner 的内存估算中按每元素 8 字节计算所有中间值；
- 依靠 date 分区限制日内张量规模；
- 通过 DAG 引用计数及时释放临时 mask/code；
- 对日频到日内广播优先使用广播 view 或支持 singleton step，避免无必要复制。

如果 profiling 显示日内 mask 成为主要内存瓶颈，再增加紧凑物理表示。

