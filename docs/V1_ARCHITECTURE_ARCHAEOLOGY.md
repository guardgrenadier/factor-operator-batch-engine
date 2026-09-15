# V1 架构考古报告（factor-operator-batch-engine）

- 日期：2026-09-07
- 目的：为"重新确认个人版需求、可能从零设计 V2"还原 V1 的问题域、实际工作方式与沉积的历史复杂度。
- 立场：本报告只描述"V1 实际是什么"，不提出 V2 架构，不把当前代码写法自动正当化。
- 证据基础：通读 `src/factor_engine/`（非 legacy）全部核心代码、`tests/`（含 `tests/legacy/`）、`data_sources.json`、根目录三份设计文档、`docs/` 现行文档与 ADR、`docs/legacy/` 提案与旧平台文档，并对交叉引用做了 rg 验证。
- 阅读顺序约定：遇到结论冲突时，按 `PROJECT_OVERVIEW_FOR_AGENTS.md` §1 的权威序（代码与非 legacy 测试 > ADR-0001 及数组布局文档 > FACTOR_ENGINE_DESIGN 未被覆盖部分 > CONTEXT.md > 其余）。

# 项目背景：

**项目一开始是公司开发的项目，由于需求多次更改，项目经历多次重构以及加功能，并且这些在重构质量不一，导致项目存在过度设计，抽象不合理，存在重复模型等问题。现在我需要基于这个项目开发一个我个人研究使用的算子引擎，因此需要先对项目进行考古。本文档是agent对项目的考古发现。在后续文档中，我会以“我：”开头进行评论，评论中可能会声明我认为要保留的需求/invariant/设计，但是，也鼓励你基于新的需求，提出架构/invariant上的新想法，使项目设计更优雅清晰高效。**

## 0. 元事实：本仓库的"历史"在哪里

**Git 历史不是开发史。** `6d170fc` 一次性导入快照，`ce5e161`（替换为最新生产快照）被 `84edfd9` 整体 revert，当前树 ≈ `f326fb7`（2026-09-03 归档同步）+ `a3b688d`（契约加固）+ `coding.md`。所有演化只能从文档考古还原，不能从 git blame 还原。（Confirmed）

证据强度标记：
- **Confirmed**：代码、测试或文档直接支持；
- **Strong inference**：多个实现事实强烈暗示；
- **Weak inference**：合理但证据不足；
- **Unknown**：无法确认。

---

## 1. Capability / Requirement Map

### C1 多公式批量提交，共享一个 Term DAG
- 用户可做：一次提交 `FormulaBatch`（common_inputs + N 个独立公式，字符串或 AST），得到各自因子。
- 真实需求：替代 legacy 的"每个特征注册 `FeatureDef`、递归求值"模式；批量计算与自动挖掘需要"一次大量独立公式、无全局命名空间"（`docs/legacy/refactor/README.md` 三场景；`算子项目汇报.md` 列出的旧痛点）。
- 需求类型：功能 + 性能（共享子表达式省算省存）。
- 机制：`FormulaBatch.bind`（作用域、禁前向/跨组引用）、名称不参与身份、结构 CSE（`compiler.py:663-672`）、未被输出引用的 source 不加载（`compiler.py:85`）、`LogicalPlan.reference_counts` 驱动释放。测试：`test_batch_engine.py:67,91`。（Confirmed）

我：对目前字符串公式+CSE的能力满意；额外需求（不是很紧急）：考虑支持写函数而非字符串（即把helper暴露为函数让用户写，因为在手写情况下用户可以利用IDE能力看到参数，而字符串不行，但现在能直接让ai根据规则写字符串，所以该需求不构成阻塞）

### C2 任务级 Domain 声明与冻结
- 用户可做：用 `DomainSpec(start,end,asset_scope,target_asset,target_freq,target_step_count)` 声明任务范围，结果由 `ResolvedOutputDomain` 精确解释。
- 真实需求：① "不向外暴露无法解释的裸数组"（FACTOR_ENGINE_DESIGN §2.6）；② 适配日频增量更新——解析输出日期前必须先知道 lookback（`design.md:34-35` 明确这是"引擎最大的问题"）；③ 坐标一致性从"运行时值自带"（legacy FeatureArray）改为"计划集中持有"。
- 需求类型：正确性 + 功能。
- 机制：`Compiler._resolve_domain`（日历裁剪、轴以"输出区间+pre_lookback"冻结、指纹 stable_hash）、`SmartQuantDataProvider.asset_codes` 冻结并拒绝二次不同请求（`smartquant.py:149-197`）。（Confirmed）

我：首先任务时间范围的需求，即start/end是必要的，这也就是说引擎既可以作为单日增量计算场景，也可以作为批量历史回填使用；关于资产范围本质上是两个需求，一是要支持多资产，以及在资产子集上计算，二是由于现在计算基于numpy，因此需要记录统一一次任务的代码和日期，方便消费结果。

### C3 有限 Lookback + 日期分块，chunk 与 whole 严格等价
- 用户可做：`ExecutionOptions(chunk_size)` 控制分区；单日输出正确回看 20 日 rolling。
- 真实需求：生产日更场景内存上界；正确性要求"分块不得改变数值"（验收标准 §22.6/7）。
- 需求类型：性能 + 正确性。
- 机制：`OperatorSpec.date_lookback`（声明式、拒绝无界，`ffill` 无 limit 编译失败 `registry.py:197-204`）、`term_lookback = local + max(deps)`、pre-lookback 预分析→轴冻结→lowering 后交叉断言一致（`compiler.py:118-121`）、`PhysicalPlanner` read/write dates（`execution.py:110-144`）、输出前缀裁剪（`execution.py:250-276`）。等价性测试极多（`test_ported_ts_operators.py:150`、`test_temporal_contracts.py:93,112` 等）。（Confirmed）

我：日更已经说过是必要的需求，此外，引入一个新需求 前向lookback，用于计算未来收益率等标签使用，但这个需求同样不是阻塞项，考虑到可能要连带设计防止未来泄露的校验等复杂度。chunk能力需要保留，chunk模式下做不了全历史指标计算，这是已知的边界。

### C4 逻辑数据引用与物理取数彻底解耦
- 用户可做：公式只写 `get_lf("stk","ClosePrice")`；换表/换字段/换后端不改公式。
- 真实需求：三代取数演化的教训（`docs/取数链路演进归档.md`）：Gen1 取数绑 Snapshot、Gen2 适配层无真批量 I/O；挖掘系统需要"改物理不改变计划语义身份"。
- 需求类型：扩展性 + 正确性。
- 机制：五层对象 `SourceRefExpr→InputSpec→SourceTerm→SourceBinding→ndarray`、`DataProvider` 五方法协议、Catalog schema v3、具名 Reader/Query Builder、LoadNormalizer 单一权威边界（ADR-0001 §4/5）。测试：`test_physical_source_group_does_not_change_logical_plan_identity`。（Confirmed）

我：实际上是一个dataprovider需求，目前字符串公式+helper定义，运行时编译取数的做法是好的；作为我个人项目，provider有复用价值，需要确保provider和 引擎解耦。目前是否理解为，字符串公式+helper是provider和引擎间的协议，字符串同时起到绑定数据源和公式定义的双重语义？这个是否要保留，是否要区分绑定和定义，请你判断提出建议。另外，目前的provider方法是好的。

### C5 日内高频数据与第三维（step）
- 用户可做：读 1/5min 源、`resample` 到日、`align_frequency` 粗到细、step 轴算子（step_mean/step_corr/日内跨日统计）。
- 真实需求：legacy 汇报中"分钟数据显式重采样、日频进场"是硬需求（`算子项目汇报.md` §7.1）；正确性动机是**禁止隐式频率转换**防止未来泄漏与语义漂移（`频率与第三维对齐规则.md`）。
- 需求类型：功能 + 正确性。
- 机制：T×N×S 模型、频率表（1min=237 step，A 股交易时段本质特性）、`_lower_resample/_lower_align_frequency` 专属 lowering 生成运行期 boundaries/step_index（`compiler.py:442-564`）。（Confirmed）

我：预期我个人只会维护1min的分钟数据，因此必须要有重采样方法；1min数据的step数还在讨论中，对于集合竞价期间是不出现（237steps）还是填0（240steps）还在考虑；禁止隐式频率/step转换这个invariant保留，但是否要求1维和N维的转换也显式写出还是允许numpy广播还在考虑中。

### C6 多资产类型与跨资产业务操作
- 用户可做：stk/cb/idx 轴共存；`project_stk_to_cb`、`select_index_feature`、`index_member_stat`、`group_*`（行业码 CODE 输入）。
- 真实需求：转债因子需正股投影；指数成分统计、申万行业分组统计是 legacy 已验证的业务场景（`算子项目汇报.md` §7.3；`指数与行业统计调用链.md`）。
- 需求类型：功能。
- 机制：helper 降为通用算子（`lookup_by_col` + 任务级 `cb.1d.underlying_stk` CODE 源（projection=axis_position）、`select_by_pos` keepdims singleton、`member_*` asset_reduce singleton）；资产选择/投影必须显式、结果匿名化（asset_type→None）。（Confirmed）

我：多资产和跨资产是必须需求，目前通过把映射所需信息作为特征，把资产间映射放在算子+特征的统一框架下做，目前选择让算子接受转债和股票在当次任务的位置映射，因此映射特征在取数阶段承担把业务映射转为位置映射的职责，这引入了一点复杂度，但这个复杂度不在这里就在算子里。这里的实现如果你有更好的方案，请提出。

### C7 显式缺失值 / 三值逻辑值协议
- 用户可做：NaN、mask 三态、比较/where/apply_mask 的可预测传播。
- 真实需求：挖掘场景大量公式会踩"truthy 猜测"和静默语义漂移；`known_bugs.md` #4 记录了 mask 被 NumPy truthy 语义破坏的历史事故。
- 需求类型：正确性。
- 机制：`ValueKind(NUMERIC/MASK/CODE)`、float64+NaN 全局协议、`as_tristate_mask`、njit 三值核、LoadNormalizer 与 Runtime 双重边界、`test_mask_semantics.py` 全覆盖。（Confirmed）

我：在引擎内部统一值协议的需求是必要的，但是否要沿用现有做法，请你判断；此外有个需求待讨论，是否要引入在float32下计算的能力，从而在大量分钟频计算中降低内存。

### C8 134 个注册算子（含别名）
- 真实需求：legacy 因子库迁移（"按实际因子迁移需求补齐旧算子"——`算子项目汇报.md`，todo #5 的"能力矩阵"动机）；性能需求催生 62 个 njit 核与 view 化（`test_operator_optimization.py`、benchmarks）。
- 机制：`OperatorSpec` 八字段契约 + Registry 构造自检（签名/动态输入一致性，`registry.py:276-320`）+ layout_rule 体系。注册名计数已程序化验证 = 134。（Confirmed）

我：算子主要涉及协议/模型的问题，这也取决于设计，需要根据新架构/invariant 讨论。

### C9 结果流与可解释输出
- 用户可做：`stream()` 流式消费、`compute()` 全量数组、`to_dataframe()`、`TemporaryFactorRepository.save` + `load_factor()` 闭环。
- 真实需求："ResultStream 后续失败时先前 chunk 不算提交"（design.md:96；legacy refactor ADR-0017）；研究侧需要 DataFrame 解释。临时仓库只为验证保存/读取语义（README 明示生产不可用，design.md:87-91）。
- 需求类型：正确性 + 功能。（Confirmed）

我：目前只实现了流式计算，还没有流式消费，因为这涉及下游消费，如何能解耦，或者说先定下流式输出协议，以后下游能明确消费，需要新设计。这个需求希望尽快实现，因为有大批量计算因子的需求。

### C10 物理 I/O 可观测与批量读取
- 真实需求：Gen2 的"load_many 只是逐字段循环"教训（归档文档 §3）；调优需要区分逻辑 load 与物理查询。
- 机制：LoadGroup 键、`measured_query/measured_arrow`、`stats.provider_events`。（Confirmed）

我：批量load已实现，这个需求肯定要实现。

### C11 `include_tables` 白名单挂载
- 真实需求：运维场景——只挂某几张表跑小任务（`examples/ops面板白名单挂载测试.ipynb`），控制初始化元数据扫描开销。（Strong inference：动机无正式文档，有具体 notebook 与测试）

我：这是因为公司存在多个数据库中字段存在重复，因此需要挂载能力，这个角度出发的需求在我个人研究不需要。但是这提醒我，目前loader会解析数据字典，这个需求现在保留为，提供数据字典方法，不初始化时自动触发，需要主动使用。

### 已被移出正式链路的 legacy 能力（刻意的负空间）
全局 Registry/alias、物化特征复用为缓存、Snapshot 离线重放、内存数据注入、字段搜索、增量 materialize——新管线**不再提供**，明确归档（`docs/取数链路演进归档.md` §8；`RepositoryDataProvider` 组合代替混入路由）。这是决定而非退化。（Confirmed）

我：这提醒了两个需求：1，内存数据注入：需要把内存中可能是其他途径算出来的特征/因子注册为数据源进入引擎内部计算的能力，这部分设计的复杂度可能是如何把外部数组/dataframe对齐到任务domain；2，自定义sql取数：比如往支持的数据库执行一个sql，返回单个特征，用于在引擎中使用要先经过算子难以描述处理的特征。

---

## 2. Architecture & Responsibility Map

### 2.1 系统本质：一条"先证明、后执行"的编译器管线 + 单一倒置端口

V1 不是"数值计算库"，也不是"数据管道"。它的整体形态是一个**数据库式 compiler pipeline**（语言→Canonical IR→多输出逻辑计划→物理计划→执行 kernel），加一个 **ports & adapters 式数据边界**（`DataProvider` 协议是唯一外部端口）。全部复杂性来自四个顶层决定，职责地图应从这四个决定读出来，而不是从目录读出来：

**决定一：语义在 compile 结束前完全闭合（"编译之后无决策"）**
- 含义：helper 展开、参数规范化、CSE、lookback、输出域冻结，全部发生在读第一个因子值之前；执行期没有任何语义决策，Runtime 无 fallback、无二次推断、无 provider 特判。
- 该决定把系统切成两个世界：**编译期 = 唯一有智能的地方**；**执行期 = 唯一机械的地方**。design.md:34-35 的"引擎最大的问题是 domain 如何解析"正是其代价——lookback 必须先于域解析、域解析必须先于绑定，于是催生"双份 lookback 计算 + 交叉断言"这一架构级缝合件。
- 责任体：语言层（formula）+ 编译层（compiler）；验证物 = LogicalPlan 的不可变性与 `semantic_id`。

我：目前确实存在lookback双重解析的问题，实现很不优雅，因此这里产生需求：lookback下沉到执行期，且借此可以模仿zipline区分每个term的lookback，这样可以只对需要回看很长的特征回看大窗口，不再统一回看窗口，为此引入的复杂度是值得的。

**决定二：公式世界与物理世界之间只有一道门（DataProvider 五方法协议）**
- 含义：逻辑身份（SourceRef/InputSpec/TermDomain）与物理身份（SourceSpec/Reader/表路径）被完整对象链隔开；"物理变化不改 `semantic_id`"是架构承诺。
- 由此推出不显眼但统摄全局的选择：**取数状态归任务，不归服务**。Provider 是任务级一次性对象，日历/资产轴/代码映射在构造-编译期冻结、拒绝二次请求；跨任务缓存被明确放弃。这是 V1 对三代取数演化（Snapshot→适配层→独立 Provider）的终态答案。
- 责任体：data_provider 包整体，但它不是"IO 模块"，而是四层子适配器体系：Catalog（语义解析）/ Reader（物理布局）/ Query Builder（SQL 方言）/ Normalizer（值协议授权）——子层之间同样是"先声明后执行"关系。

我：跨任务缓存没有需求，Provider独立，且未来有计划发展作为独立服务。

**决定三：计算核只承认位置，不承认坐标（ADR-0001）**
- 含义：Operator 层的整体职责被主动削薄为"在 T×N×S 上跑纯核"。业务坐标一致性检查（资产轴、频率、日历、fingerprint）从执行/编译主路径**撤出**：需要坐标的操作走 Compiler 专属 lowering（resample/align/select/投影），其余风险转嫁给"公式作者 + 数据产品契约"。
- 这是 V1 最重要的一次职责转移：**正确性责任从架构内部移到架构边界外部**。收益（新增算子/数据集不再穿透多层）与欠债（同 shape 错轴静默计算、CODE 列语义 retype 无类型保护）都记在这一条下。
- 责任体：operators 包；它与 Compiler 共享 `OperatorSpec` 这一唯一"双面契约"——全系统耦合最紧的数据结构。

我：目前确实是采取“信任公式作者 + 数据产品”的方式，这主要是之前项目在处理校验时遇到复杂度过高，模型重复太多，导致放弃并采取轻量语义校验。

**决定四：完整性协议优先于可用性（流式 provisional + fail-fast）**
- 含义：结果层的设计不是"如何方便拿到数组"，而是"什么情况下允许声称成功"——只有自然耗尽的流是成功的；compute 是 stream 的 collector 别名；落盘被降级为纯语义验证临时件，生产持久化整层排除在职责外。
- 责任体：execution.py 结果半区 + repository.py（"故意不做架构"的架构占位）。

### 2.2 依赖拓扑

```text
        ┌─ 语言/IR 契约（model.py 为中心轴）─────────────┐
        │                                                │
  公式语言 ──► 编译层 ──► 逻辑计划 ──► 物理计划/执行核    │
 (证明语义)   (唯一决策点)  (语义身份)     (机械执行)      │
                                │                        │
                    五方法协议（倒置点）                  │
                                │                        │
              任务级 Provider 适配器群（Catalog/Reader/    │
              QueryBuilder/Normalizer 四层子管线）        │
                                │                        │
              真实世界（OceanBase / parquet / 日历 / 轴）──┘
结果协议：Chunk ─► Stream(唯一成功判据) ─► compute / dataframe / 临时仓库
```

总体单向：`execution → compiler → {formula, model, operators, domain}`；`data_provider → {model, domain, formula(仅 SourceRefExpr)}`；`providers/repository → data_provider.normalize/readers`。**新代码零依赖 legacy**（rg 验证）；legacy 反向依赖新层的 operators/domain/SourceSpec/backend/data_sources.json——方向被文档接受（归档 §8），但仍是跨代耦合。核心不依赖任何具体 provider；具体 provider 只依赖契约。

### 2.3 架构级职责异常（不是文件级的）

- **编译层内部藏了一个小型物理层**：`_resolve_domain` 直接驱动 provider 做日历查询和轴冻结——"编译期不读数据"的公理实为"不读值、但读坐标事实"。措辞与实现的落差是决定一的固有张力，V1 用文档限定而非架构修改处理。
- **值协议有两个不对称守门人**：Source 侧单点授权（LoadNormalizer），Operator 侧 Runtime 复查——不是疏漏而是决定三的后果（核被证明为纯函数只能在输出时刻）；"唯一权威边界"的口号只覆盖一半。
- **协议外暗管**：`stats.provider_events`（getattr 别名 provider 内部 list）、`factor:` 前缀、`value_N` 别名同步、CODE=位置——这些概念没有契约对象承载，是四个顶层决定都没管到的缝隙。V2 需求确认时每一个都是"要不要升级为一等概念"的直接问题。

### 2.4 模块投影与职责明细（文件级索引，含漂移记录）

| 模块 | 实际负责 | 明确不负责 | 边界理由 | 重叠/漂移 |
|---|---|---|---|---|
| `formula.py` | 语言层：不可变 AST、受限 Parser、作用域绑定、纯 helper | 语义校验（除作用域）、数据、频率/domain | "字符串只是 Adapter" | `DEFAULT_HELPERS` 与 operator 名共同构成保留字，但 Parser 在 from_text 时刻、Compiler 在 bind 时刻各取一份，可能分叉 |
| `domain.py` | 值种类、频率/step 表、日期规范化、stable_hash | — | 全局共享叶子 | 含 5 个新管线未用的遗留对象（ExecutionScope、scope_signature、ValueSpec、DomainRef；FeatureKey 部分仍活）——前四个只被 legacy re-export；注释仍引用"Snapshot"。历史残留漂在核心模块（Confirmed） |
| `model.py` | 跨层契约（Domain/Term/Plan/Request/Result/DataProvider Protocol/错误谱系） | 任何行为 | 单一契约文件便于审计 | `ExecutionPlan=LogicalPlan`、`BatchResult=ComputeResult` 别名残留；`SourceSpec.to_dict` 新代码无调用方（Confirmed） |
| `compiler.py` | helper 展开、lookback 预分析、describe、domain 解析、参数规范化、专属 lowering、CSE、校验 | 读数据值、分区、物理路由 | design §8"不设第二个 Planner" | 3 个专属 lowering + `get_step/select_by_pos` 位置取模特判（generic 路径中的 named patch，todo #14 自认） |
| `execution.py` | PhysicalPlanner、Runtime、ResultStream、compute、Engine facade、Operator 输出校验 | 公式语义、Source 值规范化（信任 LoadNormalizer）、并行/恢复 | ADR-0001 §5 | 输出边界双重校验（有意）；`stream()` 用 `getattr(provider,"diagnostics")` 走私协议通道 |
| `operators/` | 纯 kernel + 契约声明（registry）+ 布局规则 | 参数解析（在 compiler）、坐标检查（ADR 放弃） | 契约即签名 | `alignment.py`/`timeseries.py` 留 4 个未注册死算子（broadcast_ts/broadcast_to_steps/ffill_to_finer_steps/broadcast_cs）；legacy Planner 仍会发出其中两个 → legacy 部分路径已死（Confirmed） |
| `data_provider/` | Catalog：配置校验+describe/bind；Reader：物理 I/O+坐标解释+代码身份翻译；QueryBuilder：受约束 SQL；LoadNormalizer：唯一值协议边界；smartquant：任务级日历/轴/映射冻结门面 | 语义转换、Reader 做最终数组协议、任意 SQL | ADR-0001 §4/5 两级具名注册表；"SQL 差异进 builder、布局差异才进 Reader" | `cb_stock_map` Reader 内嵌业务 SQL（JYDB 两表 JOIN、BondNature IN (10,29)），未按自家规则放 query_builder（动机成立但边界纯度妥协）（Strong inference） |
| `providers.py` | 严格内存 Provider（契约测试 + 小型任务） | 兜底猜测 | 与正式 Provider 同协议，测试即文档 | load_many 走 `dense` RawBatch 复用 LoadNormalizer——0903 修复点（todo-0902 #1） |
| `repository.py` | 临时 staging/commit + `factor:` 前缀组合 Provider | 生产存储协议 | design §18：只验证语义 | `factor:` 判断以 `startswith` 散落三处；因子绑定不携带 value_kind（恒 NUMERIC） |
| `legacy/` | 只读历史样本 | 任何新调用 | 显式隔离、不从顶层导出 | 与 `tests/legacy` 一起作为行为考古证据 |

一句话版本：**V1 = 把"证明语义"全部前移到编译期的批处理编译器，用任务级 Provider 端口隔离物理世界，用位置化数组把业务正确性推到边界之外，用流式 provisional 协议定义"成功"。** 目录结构只是这四个决定的投影。

---

## 3. Boundary / Contract Map

**B1 语言→编译：Surface AST / BoundFormulaBatch**（producer: formula.py；consumer: compiler）
- 保证：绑定后无 SymbolRef；helper 展开后只剩 Literal/SourceRef/Operator。校验：作用域在 bind，"Unknown operator/helper" 在 lowering。
- Hidden：Parser 里 helper 的 keyword 必须是字面量，**operator 的 keyword 可以是表达式**（动态 Term 输入，`formula.py:368-380`）——两类节点携带 Expr 参数的规则不对称，只能从控制流看出。`_configuration_literal` 额外允许 `neg` 包装的数字字面量当配置（`compiler.py:954-969`）。

**B2 Compiler↔Provider `describe_many`**（InputSpec）
- producer 保证：每个 ref 都有条目（Compiler 显式校验 `compiler.py:87-89`）。
- Hidden：`calendar` 只有 `"cn_a_share"`（Catalog 硬编码 `catalog.py:133`）与 `"default"`（Memory）被 SmartQuant 接受（`smartquant.py:111`）——两个别名指同一日历；**fundamental 的 `step_count=quarters`、`frequency=1d`**："日频但有 4 步"的语义不在类型系统里，只有 reader 的 `Step = quarters - rank`（`readers.py:252-254`）与规则文档知道。（Confirmed）
- Hidden：公式 `semantic_params` 不得覆盖 `PHYSICAL_DATASET_PARAMS`（`catalog.py:175-179`）。（Confirmed）

我：当时曾考虑可能有港股需求，现在目前没有。

**B3 Compiler↔Provider 轴解析（`calendar_dates`/`asset_codes`）**
- Consumer 假设：`dates` 为 `YYYYMMDD` 字符串升序；provider 对每资产只允许冻结一次（第二次不同请求报错）。"stk all = horizon 内 IfTradingDay=1 代码并集"是**已声明的临时语义**（README、design §10.2）；轴依赖公式 lookback——改一个 `ts_mean(20)` 会改资产轴（Confirmed，文档承认"已接受"）。

**B4 Compiler→LogicalPlan→PhysicalPlanner**
- 保证：拓扑序、reference_counts、read_dates = write + ≤job_lookback 前缀。
- Hidden：pre-lookback 必须等于 lowering 后 job_lookback，靠运行期 assert（`compiler.py:118-121`），不是类型保证。

**B5 Runtime↔Provider `bind_many`/`load_many`**
- Runtime 保证：一次 `load_many` 只喂同一 LoadGroup。Provider 保证：一 term 一绑定（集合校验 `execution.py:171-176`——**set 比较检测不到重复 term_id**，review_notes_response #485 已指出，现状仍在）；返回数组恰为 `T_read × N_src × S_src` float64 只读、已散布。
- 校验位置：全部在 LoadNormalizer；Runtime 只查 term_id 集合（ADR-0001 §5 明文）。

**B6 LoadNormalizer→Runtime**
- 隐含协议："空物理结果=全 Missing（或 default）"、"MASK 只 0/1/NaN、CODE 只整数/NaN"、"坐标去重在 canonical flat position 上跨批生效"（`normalize.py:210-217`）。全部有测试（`test_load_normalizer.py` 19 项）。

**B7 Compiler→Operator kernel**（最密的隐式契约群）
- 调用协议由 `spec.func` 的 Python 签名定义：前缀位置参数 + `optional_inputs` 名 = Tensor 输入，其余 = 编译期字面量配置；非空函数默认值在校验/CSE 前补齐（`compiler.py:748-812`）。**省略默认 y 与显式传 NaN 字面量会产生不同 DAG**（前者 runtime 用函数默认、后者成 LiteralTerm）——未被文档明说。（Strong inference）
- Runtime 保证 kernel 收到：float64、可能是**只读/非连续/零步长广播 view**、单例 step 的 mask（`mask_step = 0 if shape[2]==1 else s` 模式散落全部 cs 核——singleton 只沿 step 维手工广播，资产/日期维永不手工广播，属内核级约定）。
- Kernel 必须返回精确 `(T_read, N, S)`（`_validate_operator_result`），不得原地改输入（`test_code_consumers_keep_float64_inputs_immutable`、`_broadcast_inputs` 显式 `setflags(write=False)` 双保险）。

**B8 Runtime→ResultChunk / ResultStream**
- `values` 是 Workspace buffer 的只读 view（生命周期外溢给消费者——PROJECT_OVERVIEW §9.3 自认风险）；`output_slice` 以 ResolvedOutputDomain 日期轴为坐标；顺序 = 分区外层、公式内层；"只有 StopIteration 才 succeeded"。

**B9 Engine↔Provider 诊断（协议外通道）**
- `stream()` 用 `getattr(self.provider, "diagnostics", None)` 把 provider 的 list **按引用**塞进 `stats.provider_events`（`execution.py:306-308`）。不在 Protocol；同 provider 多次计算事件累积串流；`RepositoryDataProvider` 无该属性则静默失去事件。（Confirmed：隐藏 metadata 通道 + 隐式可变共享）

**B10 Catalog→Reader/QueryBuilder→Normalizer 的 `value_N` 别名**
- builders 按 bindings 顺序产列别名，normalizer 按同一顺序消费；`_aliases` 在 `readers.py:488` 与 `normalize.py:266` **各自复制一份**——两侧必须逐字节一致，无机制防分叉。（Confirmed）

**B11 CODE 的双重身份**
- 行业码/转债关系等 CODE 值，普通算子眼里只是数字，但 `group_*`/`lookup_by_col` 核把它解释为**分组 id 或轴位置**（`cb.1d.underlying_stk` 存的是"任务 stk 轴列号"，不是 InnerCode！`readers.py:289-293` + `alignment.py:35-48`）。同一列在不同层含义不同，仅靠 source 配置的 `projection` 字符串维系。文档承认（`资产轴对齐规则.md`），类型不表达。（Confirmed）

**B12 日期身份**
- 日期以 `YYYYMMDD` 字符串贯穿全链并依赖 SQL `BETWEEN` 的字符串可排序性（`新增算子与数据源指南.md` 明示）；比较、日历裁剪、ReadDomain 全部按"字典序=时间序"假设。（Confirmed）

---

## 4. Core Data Model Inventory

| 模型 | 真实概念 | 创建→消费 | 阶段 | 为何独立 | 近邻与差异性质 |
|---|---|---|---|---|---|
| `Binding/FormulaProgram/FormulaBatch` | 顺序命名表达式程序 | 用户→`bind`→Compiler | 输入 | 字符串语言的载体 | vs `BoundFormulaBatch`（绑定后只剩 Expr 映射）：真实阶段差 |
| AST 五节点 | Surface 语言 | Parser→Compiler | 输入 | HelperExpr 允许"尚待展开" | vs Canonical 三节点：SymbolRef/Helper 有意消亡 |
| `SourceRefExpr` | "公式要什么数据" | helper/parser→Compiler/Provider | 输入 | 语义身份载体 | vs `SourceSpec`：**逻辑 vs 物理**，真实领域区分（C4 能力的支点） |
| `InputSpec` | source 编译语义契约 | Provider→Compiler | 编译 | describe 与 load 的中间人 | vs `TermDomain`：类型契约 vs 具体坐标；`SourceTerm` 同持二者——设计选择非冗余 |
| `TermDomain` | 仅 SourceTerm 的业务坐标身份 | Compiler | 编译 | CSE/轴指纹需要 | vs `ArrayLayout`：ADR-0001 后差异 = **有无兼容性检查资格**，真实（历史）决定 |
| `ArrayLayout` | OperatorTerm 物理形状+提示 | layout rules | 编译 | 位置化计算最小载体 | 提示字段"不参与检查"是明文决定 |
| `DomainSpec / ResolvedOutputDomain` | 请求 vs 解析后的精确输出坐标 | 用户→Compiler→Runtime/结果 | 编译→结果 | mirror 对（"声明 vs 解析"模式） | todo #12 自认待收敛 |
| `ReadDomain` | 某分区实际读什么（含 lookback、来源 codes/steps、slice） | PhysicalPlanner/Provider bind | 执行 | 把"读≠写"固化为对象 | vs legacy `ExecutionScope`：同概念的旧版，已死（新管线零引用，Confirmed） |
| `SourceSpec / SourceBinding / LoadGroupKey` | 物理去处 / 三方关联 / 分组依据 | Provider.bind | 执行 | 使 LogicalPlan 免于携带 I/O | `load_group_key` 是 hash 字符串，诊断不可读 |
| `Term 三兄弟 + LogicalPlan(+semantic_id)` | 规范化 IR | Compiler lowering/intern | 编译 | 三类型封死是设计公理 | vs `CompiledJob`（plan+domain 打包）、`PhysicalPartition`（执行切片）：都短命且必要 |
| `OperatorSpec/VariadicInput` | 算子八项契约 | registry | 编译+执行共用 | Compiler 与 Runtime 唯一共享物 | 签名即协议→参数校验分三层（_canonical_parameters / validate_params / layout rule）——"重复 validation"但各层职责有说法（`compiler.py:840` 注释） |
| `ValueKind` | NUMERIC/MASK/CODE | 全局 | 全期 | 缺失/mask 语义载体 | vs numpy dtype：物理恒 float64，Kind 是纯逻辑层 |
| `RawBatch(mode=labels/flat/static/dense)` | Reader→Normalizer 中间协议 | readers | 加载 | 四种坐标证据等级的统一信封 | 无 schema；靠 mode 字符串与列名约定（B10） |
| `ReaderRequest` | 一次组读取的全部上下文（含冻结 axes/code_maps） | readers | 加载 | 让 Reader 无状态 | 与 legacy `DataRouter._cache` 相反的选择：状态全在 Provider |
| `ResultChunk/ResultStream/ComputeResult/ExecutionStats` | provisional 分块 / 单次流 / 全量+domain+plan+stats | Runtime→消费者 | 结果 | "完整消费才成功"协议载体 | `peak_workspace_values` 名字暗示内存但只数 dict entry（known_bugs #3 残余，文档自认） |
| 遗留：`FeatureKey`（活）、`DomainRef/ValueSpec`（死）、`ExecutionScope/scope_signature`（仅因 legacy re-export 住在 domain.py） | — | — | — | — | 差异属"实现历史"非领域（D 类候选） |

结论：模型数量大但绝大多数对应 CONTEXT.md 已命名概念且有生命周期理由（mirror 对是"声明 vs 解析"模式）；真正可疑的只有 domain.py 死模型群、两个类型别名与 4 个未注册算子。**模型多≠过度设计，本库基本成立。**

我：目前项目的隐式约定和数据模型太多，这也是我想重构项目的原因之一。

---

## 5. Critical End-to-End Flows

### F1 核心：两个字符串公式、whole-domain 计算
1. 用户构造 `FormulaBatch.from_text`（Parser 已用保留字表拒绝非法名）→ **状态：Surface AST，带 SourceSpan**。
2. `Compiler.compile`：`bind()`（SymbolRef 消失）→ `_expand_helpers`（helper→SourceRef/Operator；`project_stk_to_cb` 在此**注入新 SourceRef** `cb.1d.underlying_stk`）→ 每公式 `_expression_lookback`（不读数据、不知轴）→ 收集可达 SourceRef → `provider.describe_many`。交接保证：AST→AST；InputSpec 全覆盖由 compiler 校验。
3. `_resolve_domain`：`provider.calendar_dates`（保证有序 YYYYMMDD）+ `asset_codes(asset, axis_dates, selector)`（**依赖 pre_lookback 正确**；provider 保证冻结且拒绝变更）→ `ResolvedOutputDomain`。
4. `_lower` 递归 + `_intern`（semantic_key hash）：AST→Term DAG；CSE 发生于此；"省略默认与显式默认命中同一 Term"是此处保证。输出边界只校验 N/S ∈ {1,target}（**不校验坐标**——决定三生效处）。
5. `Runtime.execute_partition`：`bind_many(source_terms, read_domain)`（依赖 B5 恰一绑定）→ 按 load_group 分组 → 首次遇到组时 `load_many(group)`（依赖 B6：数组已只读 float64、值协议已授权）→ 拓扑调用 kernel（依赖 B7：read-domain 全长 view 进出）→ 引用计数释放（依赖确定性+无副作用声明）。
6. 输出：裁 lookback 前缀（Runtime 反向校验 planner 的 contiguous-suffix invariant，`execution.py:252-259`）→ `np.broadcast_to` → 只读 `ResultChunk` → `compute` 装配完整数组。错误模型保证：任何异常 → arrays.clear() + succeeded=False，无部分结果。

我：目前 _lower()多分枝的实现坏。

### F2 正确性脊柱：`ts_mean(close,20)` + chunk_size 分块
第 1 天输出用到 20 个 session 前历史。跨层：Compiler lookback=19 → PhysicalPlanner 重新向 provider 要完整日历（依赖 compile→execute 间日历稳定，同 provider 实例成立）定位每分区前缀 → read_dates 多 19 天 → 算子在 read 轴上运行 → 输出裁尾。**下一层依赖的核心 invariant：算子对日期的局部性恰好等于声明 lookback**；该等价性被参数化测试矩阵机械保护（`test_ported_ts_operators.py:150`、`test_temporal_contracts.py:93,112`）。轴冻结也随 lookback 变——**改公式可以改变资产轴**，本流程最反直觉的跨层耦合（文档承认"已接受"）。

### F3 stk→cb 投影
`factor = project_stk_to_cb(ratio)` → helper 展开为 `lookup_by_col(ratio, source("cb.1d.underlying_stk"))` → describe：CODE kind、cb 轴、1d → 常规 lowering + `lookup_by_col_layout`（mapping 的 asset_count 决定输出 N）→ bind：`cb_stock_map` Reader 查 JYDB、按**任务冻结的 stk 轴**把 StockInnerCode 翻译成列号（axis_position）→ static RawBatch 沿 T 广播 → Runtime njit 核按列号 gather、无效/越界→NaN。跨层保证：列号坐标系 = 本任务 stk 轴（各层不得跨任务缓存该语义——文档明文）。证据：`test_stk_to_cb_helper_registers_and_uses_the_mapping_source`、`test_stk_to_cb_mapping_uses_the_frozen_reordered_stock_axis`。

### F4 分钟源 → resample → 日频因子
`get_hf("stk","1min","ClosePrice", resample="5min", method="mean")`（与显式 `operator("resample",...)` 规范化为同一 AST——有测试钉住等价）→ Source 保持 1min/237 step → `_lower_resample`：要求 layout frequency 提示唯一且完整标准 step 轴，生成 boundaries 作 runtime 参数，输出布局只改 frequency/step_count → bind：`parquet_bars` 按天拼 DuckDB SQL，join 三个内存轴表把行映射为 flat position（secu_code 身份经冻结 code_map 翻译）→ LoadNormalizer 流式散布到 237 step → Runtime `_resample_kernel` 聚合 → 后续 `step_mean` 归约 S=1 → 输出边界 broadcast 到目标。每环有 fake-backend 临时 parquet 测试（`test_minute_fields_share_one_parquet_scan` 等）。

### F5 基本面 quarters（PIT 近似）
`get_fund("stk","Revenue")` → SourceRef 携语义参数（quarters 等）→ Catalog `describe`：frequency=1d 但 **step_count=quarters** → reader 查询 `EndDateRank<=q AND InfoPublDate>=EndDate-180d`，rank 折成 Step（rank 小=早，step 大）→ labels 散布到 T×N×Q。下游把 Q 维当 step（`get_step` 选报告期）；**quarter 新旧排序语义完全由数据产品契约保证**，引擎只当 position（`频率与第三维对齐规则.md` 明文）。同名 Item 多 data_code 需消歧（`catalog._resolve`）。

### F6 save → load_factor 闭环
`repository.save(stream)` 消费流、写 staging `.npy` chunk + metadata（dates/codes/steps 坐标落盘）→ 自然结束才 rename 发布（失败回滚本轮已发布目录——有测试）→ 新任务 `load_factor('id')`：helper→`factor:<id>` SourceRef→`RepositoryDataProvider.describe_many` 读 metadata→bind 产独立 LoadGroup→load 只读重叠 `.npy`→**同样过 `dense` LoadNormalizer**（0903 修复：不再有旁路）。语义保证：加载坐标是被保存轴超集且顺序保留。

---

## 6. Invariant Catalog

### 6.1 业务/数学
| Invariant | 保护状态 |
|---|---|
| 三值逻辑全真值表（比较缺→缺；AND False 优先；OR True 优先；not 缺→缺） | 测试（mask_semantics）+ 代码核 |
| 成员/样本 mask 的 Missing=非成员/不入选（既非 False 也非传播） | 测试 + 文档 |
| 除零/非法数学/非有限→NaN，不出现 ±inf | Runtime `_validate_operator_result` + 各核；测试 |
| 统计约定：ts_std 总体、ts_corr 样本、skew/kurt bias-corrected、ts_rank 平局中位秩 | 仅数值参考测试（隐性契约，改一处需同步多处） |
| `delay(periods<0)`=未来读取→编译失败；全系统无 future horizon | 校验（normalize_periods）+ 测试（temporal_contracts） |
| chunked ≡ whole-domain（含 NaN 位置） | 机械：大型参数化测试矩阵 |
| rolling 窗在 read 轴上局部于 lookback | 半隐式：lookback 函数与 kernel 窗口必须同步（交叉断言 + 等价测试兜底） |
| resample 只跨 S 维不跨 T 维 | 代码 + 测试 |

我：三值逻辑需要确认符合量化实践；考虑引入未来但不一定立即实现

### 6.2 架构
| Invariant | 保护状态 |
|---|---|
| Term 只有三类；SymbolRef/helper/名称不进 IR | 封死在 lowering 分派（代码），有验收 |
| 左值名与 formula_id 不参与语义身份 | 测试（不同名同式合流、CSE 系列） |
| SourceSpec 物理字段/LoadGroup/chunk_size 不进 LogicalPlan 身份 | 测试 + 约定 |
| compile 不加载值（但**读**日历/轴/catalog——措辞被文档自己限定） | 代码路径 + 文档 |
| 轴/日历冻结后任务内不变；provider 一任务一实例 | Provider 强校验（代码）+ 文档；"不可全局复用"只有口头警告 |
| Runtime 只执行已证明合法的广播，不插隐式节点 | 代码结构 + singleton view 测试 |
| `compute()` 唯一实现 = 完整消费 `stream()` | 代码结构 + 验收 |
| Workspace：非输出 Term 最后消费者后即刻释放、不依赖 gc.collect | 弱引用观察器测试（`test_batch_engine.py:342`） |
| 普通算子永不比较 asset_type/frequency/calendar/fingerprint | **仅文档/ADR**（layout_rules 注释 + ADR-0001）；测试钉行为而非规则 |

我：算子契约，如何校验输入还待讨论。

### 6.3 跨模块契约
| Invariant | 保护状态 |
|---|---|
| describe/bind/load 的"恰全覆盖、一 term 一绑定、集合相等" | 代码校验；**重复 term_id 检不出**（文档已知） |
| Source 数组：只读 float64、坐标不重复不越界、MASK/CODE 值域 | LoadNormalizer（测试充分）+ Runtime 不复查（ADR 决定） |
| kernel 返回精确 `(T_read,N,S)`、不改输入 | Runtime 校验 + immutability 测试 |
| `value_N` 别名在 builder 与 normalizer 两侧同步 | **无保护**——两份 `_aliases` 复制，纯隐式 |
| write_dates 是 read_dates 连续尾段 | Runtime 运行期校验（代码） |
| stream 顺序 = 分区×公式声明顺序 | 代码 + ordered 测试 |
| chunk 值是 view，消费者持有延长 workspace buffer 寿命 | **仅文档（§9.3）**；类型不表达 |
| `provider.diagnostics` list 存在即被当作 stats 事件通道 | **仅代码 getattr**；Protocol 无此字段 |
| CODE 值在 group/lookup 处是"位置/分组 id"、坐标系=任务冻结轴 | 仅文档 + 专项测试；类型上仍是 float |
| `factor:` 前缀识别保存因子 | 约定散在三处 startswith，无类型 |
| 日期 = `YYYYMMDD` 字符串、字典序=时间序、SQL BETWEEN 兼容 | 约定 + 各查询间接依赖 |

---

## 7. Historical Complexity / Special Cases / Open Questions

分类：A 领域本质复杂度；B 当前 abstraction 无法自然表达形成的 patch；C 历史兼容/旧需求残留；D 可能已无存在必要；E 证据不足。

| # | 现象 | 类别 | 证据强度 |
|---|---|---|---|
| 1 | ADR-0001 位置化布局 + hint 传播 + 诊断增强 = 对"每次加算子要穿多层坐标检查"的退让；`review_notes.md` #12-15 佐证"校验过多" | **B**（系统级 patch 决定）；后果（错轴静默计算）转嫁给"数据产品契约"，那部分是 A | Confirmed（ADR + 测试反向钉住） |
| 2 | 专属 lowering 三件套（resample/align_frequency/select）+ `get_step/select_by_pos` 在 `_lower_operator` 的取模特判（`compiler.py:403-414`） | 三件套=A（需 source 元数据）；取模=B，todo #14 自认应移出 Compiler | Confirmed |
| 3 | `log`=`ln`、`abs_val`=`abs` 别名；`ExecutionPlan`/`BatchResult` 类型别名；`SourceSpec.to_dict` 无消费者；`default`/`cn_a_share` 日历双名 | **C**（旧命名残留，无现行为需求） | Confirmed |
| 4 | `domain.py` 中 ExecutionScope/scope_signature/ValueSpec/DomainRef 新管线零引用（仅 legacy re-export）；注释仍写"Snapshot 存储键" | **C/D**（位置错误的历史载荷） | Confirmed |
| 5 | `broadcast_ts/broadcast_to_steps/ffill_to_finer_steps/broadcast_cs` 未注册；legacy Planner 仍发出后两个 → legacy index broadcast 路径实际已死 | **D**（新管线死代码；legacy 冻结不修） | Confirmed（registry 导入清单核对） |
| 6 | 两份 `_aliases`（readers/normalize）；双重 lookback 计算（pre-分析 vs lowering）+ 交叉断言；三重参数校验（_canonical_parameters / validate_params / layout `_position`） | 双份 lookback=A/B 混合（"域先解析还是先编译"时序逼出的双实现，V1 的正式答案）；双份 alias、双份 position 校验=B | Confirmed |
| 7 | `where(mask,x)` 省略 y 与显式 `y=NaN` 产生不同 DAG；非 provided 的 optional 输入被 `_canonical_call` 静默丢弃、由 Python 函数默认值在 runtime 补 | **B**（"签名即协议"抽象边角；CSE"省略=显式"承诺对 optional tensor 输入不成立）；文档未讨论 | Strong inference |
| 8 | `"all"` universe = horizon+lookback 内交易代码并集；副作用：改公式窗口改变资产轴 | 已声明临时语义，待权威 universe 表：**D/待迁移** | Confirmed（README、design §10.2 明文"已接受"） |
| 9 | `cb_stock_map` Reader 内嵌具体 SQL（JYDB 表名、BondNature 10/29） | **A 倾向的 B**：projection 依赖任务轴使配置化 builder 不自然，但物理 SQL 破了自家边界规则 | Strong inference |
| 10 | fundamental `publ_date_limit=-180` 与 `CumLatest` = 数据库预计算 PIT 近似；引擎层无真正 as-of | **A**（仓库形态决定）；业务充分性未知 | Confirmed / Unknown |
| 11 | 1min=237 step vs 其它=240/min：A 股交易时段事实 | **A** | Confirmed |
| 12 | legacy 提案的 per-formula 失败隔离/FailurePolicy/compute failures-map（refactor ADR-0011/0017、architecture 各 slice）从未落地；实际交付全流 fail-fast | **C**（提案残留）；fail-fast 是 design.md 重新定下的决定，"公式级独立失败恢复"在延后清单——明确延后而非遗忘 | Confirmed |
| 13 | stale 文档：`取数链路演进归档` 提 datasets.py/schema v2；`todo.md` 的 power/domain_rules 旧条目；`调用链_最新` 大段 domain_rule 章节；known_bugs #3 引用已入 legacy 的 DataRouter；`pyproject.description` 仍是 "Snapshot-based factor…engine"；FACTOR_ENGINE_DESIGN.md 首行 `/#` 手误 | **C**（文档漂移自养：PROJECT_OVERVIEW §1 已列权威序与漂移清单——罕见地诚实） | Confirmed |
| 14 | `stats.provider_events` 别名 provider 内部 list（多次计算串流、无同步）；`RepositoryDataProvider` 无 diagnostics → 组合后事件静默丢失 | **B**（协议外通道的组合失配） | Confirmed |
| 15 | `TemporaryFactorRepository`：README/design 用多段文字反复声明"生产不可用、不得扩散进 Engine/AST" | **D-风险**（防"被当作协议"的补丁式文档）；保留理由成立 | Confirmed |
| 16 | `select_index_feature` 用 layout hint `expected_asset_type="idx"` 验资产类型——hint 被 ADR 宣布"永不参与兼容性"，此处事实上参与 | **B**（有授权的特例：ADR §3 给专属 lowering 开口） | Confirmed |
| 17 | Parser 保留名在 from_text 时刻取默认 registry，Compiler bind 时刻取实际 operators——自定义算子表时两侧可不一致 | **B/D**（双入口，无测试覆盖交叉） | Weak inference |
| 18 | `examples/legacy/*.ipynb` 依赖已删除的 FeatureStoreDataProvider，已知不可运行 | **C/D**（README 已声明） | Confirmed |
| 19 | `index_member_stat` helper→三个 member 算子的词表映射；`if_then_else` = where+greater_equal 组合；`one(x)` 常量核 | helper 映射=A（业务便利）；if_then_else/one 疑似挖掘/旧因子词表迁移 | Weak inference |
| 20 | `todo-0902.md` 4 项全完成 + known_bugs 多数 P0 修复 + `a3b688d` 提交名：近期演化集中在契约硬化而非功能扩张 | 方向性事实 | Confirmed |
| 21 | 真实 OceanBase/CephFS 行为无集成测试；全绿 = fake backend + 临时 parquet；无 CI | 已知缺口（文档自认） | Confirmed |
| 22 | `RepositoryDataProvider.bind_many` 用 identity 判 `term not in factors`；factor 绑定 value_kind 恒默认 | 小瑕疵，无需求依据 | Confirmed（代码直读） |

**Unknown（无法判断）**：`benchmarks` 的 5min 生产尺寸是否代表真实负载目标；`include_tables` 白名单的实际运维方；`umr`/`cs_gauss_rank`/`location` 等算子的具体因子用途（旧汇报未逐一出现，推测为挖掘词表扩充）。

---

## 8. V1 Mental Model Summary

**大架构**：一条"语言 → 编译 → 计划 → 取数 → 执行 → 流式结果"的单向管线，全入口共用。字符串公式经作用域绑定与 helper 展开成三类节点的 Canonical AST；Compiler 在任何数据加载之前完成参数规范化、专属 lowering、CSE、lookback 分析并冻结输出域；Runtime 只做"按分区、按拓扑序、在 `float64 T×N×S` 数组上调用无副作用 kernel"。数据侧只有一个五方法 `DataProvider` 协议：公式世界永远不知道表和文件。`legacy/` 是上一代研究平台（FeatureDef/FeatureArray/Snapshot Store/Router）的封存标本，新层零依赖。

四个顶层决定的速记：① 语义在编译期完全闭合（之后无决策）；② 物理世界只隔一道端口、取数状态归任务；③ 计算核只认位置不认坐标（正确性责任外移到数据产品契约，ADR-0001）；④ 完整性协议优先（流式 provisional + fail-fast，compute=stream）。

**核心数据模型**：`FormulaBatch`（任务）、`Term DAG/LogicalPlan`（语义身份）、三域对象（`DomainSpec/ResolvedOutputDomain`、`TermDomain/ArrayLayout`、`ReadDomain`）、`OperatorSpec`（签名即契约）、`SourceBinding+LoadGroup`、`ResultChunk/ComputeResult`。镜像对（Spec→Resolved）是"声明 vs 解析"模式而非冗余；死载荷集中在 `domain.py` 遗留对象、两个类型别名与 4 个未注册算子。

**最重要的数据流**：文本 → 在"输出区间+公式 lookback"上冻结日历/资产轴（**改公式可改轴**）→ 绑定/分组读取 → 在含 lookback 前缀的 read 轴上算 → 裁尾广播成只读 view → 流式/装配输出。跨层暗线：cb 投影携带任务轴**位置**而非代码、基本面把**报告期 rank 折成 step**、`value_N` 别名靠两侧同序复制维系。

**最重要的 invariant**：名称不进身份；物理变更不改 `semantic_id`；负 periods/无界历史必编译失败；chunk ≡ whole；kernel 确定性、不改输入、返回精确 read 域形状；MASK∈{0,1/NaN}、非有限→NaN；轴冻结不可变；compute=stream 唯一路径。其中"普通算子不做坐标检查"与"provider 一任务一实例"最容易被无意破坏（只活在文档与测试行为里）。

**当前仍无法完全解释的设计**：`where` 省略 optional 输入与显式默认在 CSE 身份上的不对称；Parser/Compiler 两套保留名来源的时序关系是有意还是巧合；`index_member_stat`/`if_then_else`/`one` 等词的原始需求方（推测为旧因子与挖掘词表，无直接文档）；`peak_workspace_values` 为何在 0903 修复轮之后仍停留在 entry 计数；提案阶段承诺过的 per-formula 失败隔离被 fail-fast 取代的完整决策记录（design.md 只有结论没有过程）。

**给 V2 需求确认的使用提示（不是设计建议）**：`coding.md` 表明项目主人已把 special case、兼容层、隐式契约的累积当作受控风险跟踪——本报告 §7 的 B/C/D 条目大多在 todo/known_bugs 体系内留有自认痕迹，可连同 `算子项目汇报.md`（legacy 已验证业务场景）、`review_notes.md`（用户亲自提出的校验过重问题）、refactor ADR（未被实现 vs 被实现的清单）一起，直接当作用户需求的第一手证据使用。
