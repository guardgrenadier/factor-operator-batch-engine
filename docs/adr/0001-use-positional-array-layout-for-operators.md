---
status: accepted
---

# 普通算子采用按位置的数组布局契约

普通算子只根据 NumPy shape 和广播规则计算，不再比较输入的资产类型、代码顺序、
frequency、calendar 或 axis fingerprint，也不再通过 `domain_rule` 为每个中间 Term
维护业务坐标身份。我们明确接受“shape 相同但业务坐标不同”的输入按位置计算，
以换取更小的算子契约、更少的编译期分支和更容易扩展的 Operator Registry。

## Consequences

- Domain 只在 Source 描述与绑定、ReadDomain 和最终 OutputDomain 边界保持权威；普通
  OperatorTerm 的中间值只具有 `T × N × S` ArrayLayout。
- 日期维必须覆盖当前分区；资产维和第三维只要求 NumPy 可广播，即相等或一侧为 1。
- shape-changing 算子只声明结构性的 layout effect，不得借此恢复业务坐标兼容性检查；
  `resample`、显式频率转换和资产选择等确需元数据的操作保留专属 lowering。
- 最终输出只检查并广播到请求的物理 shape，不根据中间值的来源证明业务坐标一致。
- 相同长度但代码顺序、资产类型、频率或 step 含义不同的数组可能静默逐位置计算；
  这种对齐正确性由公式作者和数据产品契约负责。
- 该决定替代正式设计和对齐规则中“shape 相同但坐标身份不同必须编译失败”的旧契约。
