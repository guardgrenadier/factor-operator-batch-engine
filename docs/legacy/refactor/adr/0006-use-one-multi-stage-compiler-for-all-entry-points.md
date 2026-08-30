# ADR 0006：所有入口使用同一个多阶段 Compiler

- 状态：已接受
- 日期：2026-07-24

## 背景

系统同时提供研究 helper、`FeatureDef`、字符串批量公式和自动挖掘入口。当前实现中，部分 helper 在 `manager.py` 生成 Expr，`FormulaParser` 解析字符串并展开部分 helper，`FeatureManager.register()` 规范化定义，`Planner` 再进行 alias、mask、delay 和空间改写。这容易形成多个彼此重叠的“编译点”。

## 决策

所有入口先汇入同一种 Surface AST，再由唯一的 Compiler 门面完成多阶段编译：

```text
研究 helper / FeatureDef -> 研究适配层 --\
                                         -> Surface AST
字符串公式 -------------> Parser -------/
                                              |
                                              v
                                      单一 Compiler
                                      -> Canonical IR
                                      -> InputRequirements
                                      -> LogicalPlan
```

Compiler 内部允许多个顺序固定的 pass，但不存在多套独立的编译器。

## 概念边界

- 研究 helper：构造 Surface AST 或研究定义，不生成执行计划。
- DSL macro：属于公式语言，由 Compiler 展开，不直接执行。
- Runtime 算子：Canonical IR 中可执行的明确节点。
- 研究适配层：把 alias、`FeatureDef.delay_lf`、mask 等研究便利状态展开为显式 Surface AST；不做数据读取或执行规划。
- Domain Lowering：按 ADR 0005 展开唯一投影，不添加 PIT 政策。
- LogicalPlan：任务级多输出 DAG，不含 chunk 和 worker 决策。
- PhysicalPlan：结合资源和后端能力决定执行分区，不改变公式语义。

## Compiler 的阶段

具体实现可以调整，但职责顺序应保持：

1. 将字符串解析或直接接收为 Surface AST；
2. 绑定算子名、逻辑数据 key 和任务内符号；
3. 展开正式 DSL macro；
4. 根据输入规格和 `ExecutionDomain` 推导空间；
5. 展开唯一的资产与频率投影；
6. 执行通用可执行性校验；
7. 生成 Canonical IR；
8. 提取完整 `InputRequirements`；
9. 合并批内输出并构造 LogicalPlan DAG。

## 影响

- `FeatureManager.register()` 不再拥有一套独立公式规范化逻辑。
- 研究 helper 和字符串公式产生的等价表达式会进入相同的 Canonical IR。
- Compiler 输出应保留 Surface AST、Canonical IR 和展开记录，支持解释与复现。
- 编译批次以整体构造多输出 DAG，为叶子复用和公共子表达式消除提供统一位置。
- PhysicalPlan、分块和多进程不属于 Compiler 的语义阶段。

