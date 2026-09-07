# ADR 0008：三个场景使用同一种语义 ComputeRequest

- 状态：已接受
- 日期：2026-07-24

## 背景

交互研究、已知公式批量计算和自动因子挖掘有不同的上层工作流，但都需要在一个统一计算域中执行一组公式。如果在引擎请求中携带场景 mode、`FeatureDef`、Router、物化选项或 worker 参数，核心行为会再次依赖入口和基础设施。

## 决策

所有入口最终构造同一种 ComputeRequest：

```text
ComputeRequest
├── ExecutionDomain
└── FormulaItem[]
    ├── id
    └── expression
```

- ExecutionDomain 描述这一批输出共同的资产、频率、日期区间和资产轴。
- FormulaItem.id 只需在任务内唯一，用于关联输出与诊断。
- expression 可以由字符串 Parser 或研究 helper 构造的 Surface AST 提供。
- ComputeRequest 不包含场景 mode。

资源和环境采用独立协议：

```text
ComputeRequest   -> 算什么
ExecutionOptions -> 如何使用计算资源
DataProvider     -> 从哪里和如何读取叶子数据
ResultSink       -> 结果去哪里
```

## 影响

- 研究适配层在提交前展开 `FeatureDef`、alias、helper、delay 和 mask 便利语义。
- 批量计算和自动挖掘直接把字符串公式包装为 FormulaItem。
- worker 数、chunk、内存预算和执行后端不参与公式语义签名。
- 物化、overwrite 和结果路径不进入 ComputeRequest。
- 引擎无需根据 research、batch 或 mining mode 改变编译行为。

## 尚未决定

- ExecutionDomain 如何引用不可变日期轴和资产轴。
- FormulaItem 的序列化格式与可选业务 metadata。
- ComputeRequest 的外部 wire format。

