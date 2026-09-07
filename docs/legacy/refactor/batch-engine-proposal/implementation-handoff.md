# 统一批量因子引擎完整实现 Handoff

- 状态：待实现
- 日期：2026-07-31
- 下一阶段目标：将当前核心验证切片演进为项目唯一计算引擎，并删除旧
  `Planner/Executor/Calculator` 计算路径

## 1. 终局目标

交互式研究、单因子计算、已知公式批量计算和自动因子挖掘必须共用同一套
编译与执行内核：

```text
单因子研究 = FormulaBatch 大小为 1
多因子计算 = FormulaBatch 大小大于 1
```

最终不保留两套计算语义或两套 Runtime。`FeatureManager` 可以作为研究定义和结果
物化的上层 facade 保留，但它必须调用唯一的批量引擎，不再递归计算定义。

## 2. 权威设计资料

本文不重复已确认的详细设计。后续 agent 应先阅读：

1. [批量引擎逐条设计决策](decisions.md)
2. [批量引擎架构设计](architecture.md)
3. [远期 ADR 目录](../adr/)
4. [FeatureRegistry、ExecutionRequest 与 SourceExpr 设计](../../fix/feature-registry-execution-request-source-expr.md)
5. [重构总览](../README.md)

冲突时优先级为：用户最新确认 > 已接受 ADR > `decisions.md` 中已决定项 >
架构草案 > 当前过渡实现。

## 3. 当前实现现状

### 3.1 新内核已验证

`core/batch_engine.py` 已实现一个可运行的核心闭环，具体范围见
[architecture.md 的实现状态](architecture.md#111-核心验证实现状态2026-07-31)。关键代码为：

- `core/batch_engine.py`：FormulaBatch adapter、Term DAG、CSE、bind、拓扑执行和 Workspace；
- `core/ops.py`：五字段轻量 `OperatorSpec`；
- `core/schema.py`：`ValueKind/ValueSpec/DomainRef`；
- `core/data_router.py`：`read_spec()` 和 `load_many()` 单字段 fallback；
- `tests/test_batch_engine.py`：当前核心验证用例。

最近一次验证结果为 `24 passed`，同时通过相关文件的 Ruff 检查和
`git diff --check`。

### 3.2 旧引擎仍在使用

`core/engine.py` 当前同时包含：

- 新旧路径共用的 `Expr/FormulaParser/helper expansion`；
- 旧 `Planner/Executor/Calculator`。

`core/manager.py` 的 `FeatureManager.execute()` 仍递归展开注册依赖，并调用旧
`Calculator`。新引擎反过来从 `core/engine.py` 导入 Expr 和 Parser，因此旧文件尚不能
直接删除。

### 3.3 工作树注意事项

当前工作树存在用户的未提交改动，包括 Registry、Manager、Engine、Schema、文档和测试。
后续工作必须增量编辑，不得回退、覆盖或清理与当前任务无关的改动。

## 4. 完整替换旧引擎的缺口

### 4.1 Registry 定义图与 FactorRef

当前 `BatchCompiler` 把所有 dotted key 都 Lower 为 SourceTerm，无法区分外部数据与注册
因子。必须增加注册因子引用节点，并在进入 Domain Lowering 之前完成：

- alias 冻结和 canonical key 解析；
- 未物化 FeatureDef 的递归展开；
- 任务内定义的共享 DAG 合并；
- 循环依赖诊断；
- 明确选择读取已物化数据时，把它绑定为 Store SourceTerm。

### 4.2 ResolvedExecutionDomain 和 DomainCatalog

当前 `DomainRef(asset, freq)` 只能支持同域验证。必须落实已确认的：

- `DomainSpec`、`AssetSelectionSpec` 和 `ResolvedExecutionDomain`；
- `start/end/target_asset/target_freq`；
- 任务涉及的所有资产轴、日期轴和 step 轴；
- 稳定排序和 fingerprint；
- 动态 universe 的区间并集轴与 mask SourceTerm；
- 日历、codes、资产关系和 code 编码的 DomainCatalog 端口。

### 4.3 Domain Lowering

必须把已接受 ADR 0005 的对齐矩阵实现为独立 Compiler pass，不逐行复制旧
Planner：

- 同域直接计算；
- 只自动 Lower 唯一、非聚合投影；
- 日频到日内只广播，不隐式添加 delay；
- 细频到粗频和多对一资产映射需要公式明确 reducer/selector；
- idx 必须明确选择后再广播；
- AlignmentRule 插入的显式 OperatorTerm 直接获得目标 `domain_ref`，
  OperatorSpec 不恢复 `infer_domain`。

### 4.4 Source InputSpec、bind 和真实批量读取

当前外部输入默认为 numeric，依赖调用方手工传入 `source_kinds`。完整实现应当：

- SourceCatalog/DataRouter bind 产生 `SourceBinding + InputSpec`；
- InputSpec 明确 ValueKind、shape/step、missing、源 domain 和读取语义；
- mask/code 类型来自数据目录或 helper 显式声明，不依赖任务级旁路字典；
- LoadGroupKey 包含 dataset identity、read domain、snapshot/version 和查询语义；
- 同表多字段由 DataRouter 后端一次读取，并对每个 Term 执行坐标、shape、dtype 和
  missing 校验；
- Store、ArtifactStore、database 和 memory 都通过同一 DataProvider 端口绑定，Runtime
  不再 fallback 搜索。

### 4.5 Value 协议与算子合规

当前 prototype 只在算子返回后强制转为 float64，不等于完成 ADR 0012。必须：

- 全部 Runtime 值使用 float64 + NaN；
- mask 保留 `1.0/0.0/NaN` 三值语义，逻辑算子不得把 NaN 当作 True；
- code 经 DomainCatalog/DataProvider 编码为 float64，字符串不直接进入 Runtime；
- Source 和 Operator 输出均按 Term domain 校验完整 `T x N x S` shape；
- sample mask、group code、member mask 等数组必须是显式 Term 输入，不隐藏在 Runtime
  kwargs；
- 保持当前五字段 OperatorSpec，不为这些语义恢复 ParamSchema 或
  `infer_domain`。

### 4.6 Helper、mask 和 delay 规范化

`get_lf/get_hf/get_fund`、行业统计、指数选择和重采样必须与字符串 DSL 共用同一 Expr
builder。推荐利用项目未上线的窗口清理旧隐式语义：

- helper 直接产生 SourceRef、FactorRef 和显式 Operator Expr；
- `input_mask/output_mask` 在定义规范化阶段展开为 `apply_mask`；
- sample mask 展开为 sample-aware 算子的显式 mask 输入；
- 删除日频到日内的隐式 `delay_lf/delay_dict`语义，delay 由公式/helper 显式生成；
- 不在 Runtime 中读取 helper metadata 或研究状态。

### 4.7 PhysicalPlan、分区和 Workspace

当前只验证 whole-domain/scope 执行。要替换 FeatureManager 现有 chunk 路径，必须：

- LogicalPlan 与不可变 ExecutionPlan 分层；
- 根据日期分区和任务级 lookback 生成 PhysicalPlan；
- 每分区区分 read dates 与 write dates；
- 保持现有依赖引用计数释放，且分区之间不泄漏 Workspace 状态；
- 完整输出由结果组件装配，公开 API 不返回要求用户自行拼接的 chunk；
- 分区数据加载、结果写入和缓存的生命周期明确。

### 4.8 ComputeResult、ResultAdapter 和长期物化

必须实现已接受的结果边界：

- ComputeResult 携带完整 ResolvedExecutionDomain；
- memory assembler 按 formula_id 组装完整数组；
- DataFrame 转换放在 ResultAdapter；
- disk output 显式提供 `formula_id -> DatasetKey` 映射；
- 每个公式使用 staging，成功后 finalize，失败时不暴露部分结果；
- 长期存储语义按 ADR 0021–0026 实现，不把任务固定轴写成数据集全局轴。

### 4.9 唯一公开 API 与旧代码删除

完整入口应以已确认的 ComputeRequest 为语义核心，不以当前单目标
ExecutionRequest 为内核协议。研究 facade 可以将单目标请求转换成大小为 1 的
FormulaBatch。

切换完成后：

- Expr/Parser/helper expansion 从旧 `core/engine.py` 拆到独立前端模块；
- 新引擎成为唯一 `engine.py` 公共实现；
- `FeatureManager.execute()` 只做 Registry/Request/Result 适配；
- 删除旧 Planner、递归 Executor、Calculator 和 `runtime_features`；
- 删除 `infer_date_overlap()` 的算子名硬编码。

## 5. 仍需要讨论或确认的边界

### BOUNDARY-001：注册因子引用语法

已决定“裸 dotted key = `source(key)`”，因此不能再根据 Registry/Store/DataRouter 中能否
找到 key 来猜测它是因子还是 source。

建议新增显式 `factor("stk.1d.alpha")`/FactorRef：alias 在注册时冻结为 FactorRef；
SourceRef 和 FactorRef 在 Surface AST 中始终分离。需要用户确认 helper 命名。

### BOUNDARY-002：FactorRef 的展开与物化优先级

需要决定 `factor(key)` 遇到同时存在 Registry definition 和已物化 dataset 时的策略。
建议不做隐式新鲜度判断：

- `factor(key)` 表示展开当前 Registry 定义；
- 读取长期物化数据使用明确 `dataset(key)`/materialized FactorRef policy；
- 是否允许“已物化则自动读取”必须作为请求策略显式声明。

### BOUNDARY-003：旧 FeatureDef 计算字段的去留

需要确认是否删除 `input_mask/sample_mask/output_mask/delay_lf/delay_dict`。建议利用尚未上线
直接收敛：

- mask 在 Registry 规范化时展开进公式；
- delay 改为公式/helper 显式语义；
- 完成定义迁移后删除这些字段，不在新 Engine 增加兼容分支。

### BOUNDARY-004：Source ValueKind 和 InputSpec 的权威来源

需要确认 ValueKind 是 DataRouter 数据字典的必填字段，还是允许 helper 覆盖。建议：

- SourceCatalog 提供默认 InputSpec；
- helper 只能在与物理字段协议兼容时缩窄语义，不能任意把 numeric 重解释为 code；
- bind 阶段是最终校验点；
- 删除当前 `BatchFactorEngine.source_kinds` 旁路参数。

### BOUNDARY-005：分区策略

`decisions.md` 的 OPEN-006 仍未决。建议完整替换的第一版先使用显式固定 date
chunk，在请求级设置；保留按内存预算生成分区的 PhysicalPlanner 接口，但不在没有工作负载
数据时实现自适应算法。

### BOUNDARY-006：DataProvider 错误和 snapshot 一致性

`decisions.md` 的 OPEN-014 仍未决。需要定义 bind 失败、整组 load 失败、单字段缺失和坐标
不匹配的结构化错误，并决定同组部分失败是否允许返回。还必须明确一个任务的数据版本/
snapshot 是否在 bind 后冻结。

建议首版：bind 冻结 snapshot token；同组任一必需字段失败则该 LoadGroup 失败；再按
LogicalPlan 依赖归因到 formula_id。

### BOUNDARY-007：公开请求和研究 facade

需要确定当前 `ExecutionRequest` 是删除还是作为研究 adapter 保留。建议：

- Engine 只接收统一 ComputeRequest；
- ComputeRequest 包含 DomainSpec、FormulaItem[] 和语义输出配置引用，不携带 Registry 状态；
- FeatureManager/ResearchFacade 将 alias、FeatureDef 和单目标请求转换为 ComputeRequest；
- 项目未上线，可在切换后删除 ExecutionRequest，也可暂保留一个无计算语义的薄 adapter。

### BOUNDARY-008：长期 ArtifactStore 与当前 FeatureStore

需要明确“完整实现”是否同时替换当前 snapshot-bound FeatureStore，还是先通过
ArtifactStore port 使其作为过渡 adapter。

建议以 ADR 0021–0026 为终局契约，但分离两个删除门槛：新引擎替换旧计算路径只要
ArtifactStore port 稳定且当前 FeatureStore adapter 满足契约；长期分区存储内部可在同一目标
架构下继续替换，不恢复第二套 Engine。

### BOUNDARY-009：失败隔离的首次切换范围

远期失败语义已由 ADR 0011、0017 和 0018 确定，实现细节见 OPEN-010。需要决定在
删除旧引擎前是否必须实现 `continue_independent`。

建议不把它设为旧引擎删除阻塞项：首次统一切换保持已接受的 fail-fast；但错误类型、
formula_id 归因和结果 staging 结构必须先按远期协议建好，避免后续破坏 API。

### BOUNDARY-010：初始 Workspace

`decisions.md` 的 OPEN-011 仍未决。建议统一切换版只允许经过 InputSpec/domain 校验的任务内
InputBinding 和测试注入，不引入 Engine 实例隐藏跨任务缓存。

### BOUNDARY-011：多行公式文本协议

`decisions.md` 的 OPEN-009 仍未决。建议以 `FormulaItem[]` 为唯一核心协议；当前
`formula_id = expression` 仅作为 adapter v1，允许未来替换为支持跨行公式的 parser，不影响 Compiler。

## 6. 建议的目标模块边界

```text
core/
├── expr.py
│   └── Expr / Parser / source() / factor() / helper expansion
├── domain.py
│   └── DomainSpec / ResolvedExecutionDomain / DomainResolver / DomainCatalog port
├── compiler.py
│   └── RegistryResolver / canonicalization / Domain Lowering / Term DAG
├── terms.py
│   └── Term / ValueSpec / SourceBinding / ExecutionPlan
├── runtime.py
│   └── PhysicalPlan / Scheduler / TermExecutor / Workspace
├── provider.py
│   └── DataProvider port / DataRouter adapter
├── results.py
│   └── ComputeResult / memory assembler / disk writer / DataFrame adapter
├── engine.py
│   └── 唯一公共 Engine facade
├── registry.py
│   └── 研究定义和 alias
└── manager.py
    └── 可选研究 facade，不包含计算内核
```

模块名可调整，但职责分层不应退回到一个巨大 `engine.py`。

## 7. 建议实施顺序与验收点

### M0：特征化测试与共享前端拆分

- 为旧引擎现存语义增加特征化测试，明确哪些保留、哪些按新 ADR 有意改变；
- 将 Expr/Parser/helper builder 拆出 `core/engine.py`；
- 保持当前 24 项测试通过。

验收：新旧路径都只从共享前端导入 AST，没有复制 Expr 类型。

### M1：FactorRef 和 RegistryResolver

- 实现 FactorRef 语法和序列化；
- 一次展开整个目标定义图；
- 未物化依赖并入同一 FormulaBatch；
- 循环、缺失定义和物化选择产生结构化诊断。

验收：单因子、多层注册依赖和多输出共享子图通过同一 Compiler。

### M2：DomainResolver 和 Domain Lowering

- 实现固定 ResolvedExecutionDomain；
- 实现已确认对齐矩阵；
- 显式投影 Term 包含目标 domain；
- 对模糊/多对一转换给出可操作诊断。

验收：同域、stk→cb、daily→intraday、显式 fine→coarse、idx 选择广播都通过
Canonical Expr 快照和数组结果测试。

### M3：InputSpec、Value 协议与 DataProvider

- 取消手工 `source_kinds`；
- 完成 numeric/mask/code 规范化；
- 实现真实 LoadGroup 绑定与坐标校验；
- 为至少一个常用后端实现同表多字段批量读取。

验收：`get_lf(if_sus=True)`、基本面 code/quarters、group/member 和 mask missing 测试
通过；同表多字段只触发一次 provider query。

### M4：PhysicalPlan 和结果组件

- 实现日期分区、任务 lookback 和输出裁剪；
- 实现 ComputeResult + ResolvedExecutionDomain；
- 实现 memory assembler、DataFrame adapter 和 disk result handler；
- 接入 staging/finalize/abort。

验收：whole-domain 与 chunk 的结果一致；失败不暴露部分公式结果；内存和磁盘
结果共享相同 domain 契约。

### M5：研究入口切换和旧引擎删除

- FeatureManager 将单因子转为 FormulaBatch 大小 1；
- 全部 helper 只生成新 AST；
- 物化、overwrite 和 return-array 使用新 Result 组件；
- 删除旧 Planner/Executor/Calculator 和专用 overlap 推导。

验收：代码中不再存在第二套计算路径；单因子、批量因子和注册因子均经过
同一 `compile -> execute`。

### M6：远期完整语义

- 按远期 ADR 增加 formula 依赖失败传播和 `continue_independent`；
- 完成 ArtifactStore 长期分区数据集实现；
- 加入分区级轻量 RuntimeReport；
- 使用真实 workload 数据决定自适应分区、并发、重试和内存优化。

## 8. 旧引擎删除门槛

只有同时满足以下条件时，才删除旧路径：

- 所有公开计算入口均构造 ComputeRequest/FormulaBatch；
- 单因子是大小为 1 的批次，不再有专用 Runtime；
- Registry 依赖在编译前完整展开，Runtime 没有 `runtime_features`；
- DomainResolver 和对齐矩阵覆盖所有保留场景；
- Source 不依赖 `source_kinds` 或 Runtime fallback；
- whole-domain 与 chunk 结果一致；
- 返回数组、DataFrame 和物化共用同一 ComputeResult/domain 契约；
- `get_lf/get_hf/get_fund`、行业、指数、stk→cb、频率对齐和 mask 语义都有测试；
- 不再存在对旧 Planner、递归 Executor 或 Calculator 的导入；
- 相关设计文档和用户文档已更新为唯一路径。

## 9. 建议测试矩阵

开始大规模迁移前，先将下列场景固定为可执行契约：

- Parser：单行/多行、source/factor helper、参数规范化和错误定位；
- Registry：alias 冻结、多层定义、循环依赖和物化选择；
- DAG：跨公式 CSE、结构身份、拓扑顺序和引用计数释放；
- Domain：同域、唯一投影、模糊投影拒绝、idx 选择和显式重采样；
- Value：numeric/mask/code、NaN mask 真值表、shape 和 missing 校验；
- Source：同表多字段、不同查询语义分组、snapshot 冻结和加载失败；
- Lookback：多层时序算子、分区 overlap、无界窗口拒绝和输出裁剪；
- Result：memory/disk/both、DataFrame、失败清理和 formula_id 到 DatasetKey；
- 端到端：单因子、多因子、未物化依赖、基本面、停牌复权、转债、指数和日内。

## 10. 下一位 agent 的首个任务

不要立即删除旧引擎。先完成 M0，并将 BOUNDARY-001 至 BOUNDARY-004 中会影响 AST、
Registry 和 InputSpec 的决定与用户确认。推荐的第一个可交付变更为：

1. 新增独立 Expr/Parser 模块，保持语义不变；
2. 新增 FactorRef 的最小类型、序列化和 parser/helper 草案；
3. 增加 RegistryResolver 的失败用例和接口骨架；
4. 不改动 Runtime 和 Domain 逻辑；
5. 待引用语义确认后再完成 M1。

## 11. Suggested skills

- `$handoff`：下一个长会话结束时继续记录完成项、未决边界和工作树状态。
- 当前可用的 Athena 业务 skills 面向因子研究业务流程，不适用于本地计算引擎重构；
  本任务不建议调用它们。

## 12. 建议验证命令

```bash
.venv/bin/ruff check core tests
.venv/bin/python -m pytest -q
git diff --check
```

全量 Ruff 当前可能会报告已有 notebook 的 `E402`；若本轮未修改该 notebook，不要为清理
无关历史问题而扩大改动范围，可先对本轮相关 Python 文件执行 Ruff。
