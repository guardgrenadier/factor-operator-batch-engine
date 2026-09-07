# ADR-0001: 普通算子使用位置化数组布局（ArrayLayout）

- 状态：已接受
- 日期：2026-08-31
- 设计说明：[数组布局与数据加载边界设计](../数组布局与数据加载边界设计.md)

## 背景

普通算子原先通过 `TermDomain`/`domain_rule` 检查输入的资产类型、代码及顺序、
frequency、calendar 与 axis fingerprint。这让每次新增算子或数据集都要同时触及
编译、读取与运行时多层代码；同时各 Reader 与 Runtime 还重复执行数组转换与校验。

## 决策

1. **普通 OperatorTerm 只携带 `ArrayLayout`**：物理形状 `asset_count`/`step_count`
   加上不参与兼容性检查的溯源提示（`asset_type`/`frequency`）。N、S 分别按
   NumPy 广播规则合并，T 恒等于当前物理分区日期长度。相同 shape、不同业务坐标
   的输入明确允许逐位置计算。
2. **溯源提示只用于诊断与专属 lowering**：提示在无歧义时沿计算图传播（混合即
   失效为 None）。资产维不可广播且两侧提示唯一不同时，编译错误附带两侧资产
   类型、N 与显式转换建议；否则退回普通 shape 错误。提示永不进入兼容性规则，
   也不把 asset type 放回 `domain_rule`。
3. **改变 N/S 或依赖 Source 元数据的操作保持显式**：资产选择、股票到转债投影、
   step reduction、`resample`、`align_frequency` 使用专属 lowering 或有限
   layout effect，从布局提示读取所需信息，不恢复业务坐标兼容性检查。
4. **Reader/Query Builder 两级具名注册表**：SQL 数据集统一由 `sql_reader`
   执行，SQL 差异收敛到具名 Query Builder（panel_fields/adjust_factor/
   untradable）；fundamental 与 cb_stock_map 因结果结构与任务依赖保留具名
   Reader；parquet bars 走 `parquet_bars`。配置 schema_version 3 强制显式
   声明 `reader`/`query_builder`，不提供任意 SQL DSL 或插件框架。
5. **LoadNormalizer 是 Source 数组进入 Runtime 前的唯一权威规范化边界**：
   坐标解析与校验、散布、float64、NULL/缺失/Infinity→NaN、MASK/CODE 值域、
   显式默认值与静态日期广播集中于此。Runtime 信任已规范化数组，只检查
   load_many 返回的 term_id 集合；`_validate_operator_result()` 继续校验
   算子返回值的 shape、dtype、Infinity 与 ValueKind 契约。

## 后果

- 公式作者与数据产品契约负责保证位置含义；引擎只保证数组运算合法，错误的
  业务坐标组合可能静默计算。
- 新增数据集在既有 Reader/Query Builder 覆盖范围内只需配置即可接入。
- 每个 Source 数组只做一次完整规范化与校验，消除了 Reader 与 Runtime 的重复
  扫描。
