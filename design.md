因子公式体系分层：
- 引擎内部认literal，source，operator 3个term
- 外部通用协议，包括以后如果有的自动挖掘算法，是自有的 ast expr
- 对于上游研究或批量计算场景，以字符串为载体；字符串先解析为featureexpr/helperfunction/operator/literal；随后planner进行展开helper，对齐资产、频率，变成纯算子+特征；后续引擎内部compiler再编译为3种term

featureexpr包含对其他特征的引用和source引用，如何区分featureexpr和sourceexpr？
- 取数helper显然应该负责把复权停牌等展开为算子的同时注册为sourceexpr
- 是不是ast expr 和 engineterm需要一一对应？

因子/公式定义的链路：
- 研究时定义原始特征：helper直接解析为 ast expr，注册原始特征为source
- 研究时定义公式/中间变量：helper/formula解析字符串，解析为ast expr
- 批量计算时定义原始特征：helper取数
- 批量计算时公式，解析多行公式适配器，helper/formula解析为ast expr

目前 字符串公式/ast expr 与 source/source exper/sourcespec 之间关系不明确
- 是否需要统一为helper只解析成字符串，后续parse统一解析为ast expr？
- 但似乎语义上应该尽早转化为ast expr
- source expr 和 sourcespec 关系是什么？

此外，还有命名方面的问题：
- 字符串公式“intermediate=divide(ts_mean(close, 5), vol)” 中的intermediate应该如何解析
- helper是否也要支持 close = get_lf()？

总之，目前 字符串公式/ast expr/engine term的分层已经明确，具体如何解析仍待讨论；ast expr 到 engine term 中间需要经过planner，这是明确的。

计算引擎内部：
- 先把 ast expr 解析为 term
- 根据term构建DAG图，阻塞点可能是如何解析命名引用从而构建依赖
- 模仿zipline，拓扑排序逐个计算，source用到时同表多字段读取，读取lookback简化为统一读最大的区间，用不到这么长时offset
- workspace储存array，做生命周期管理，没有后续引用时释放内存
- 分块也模仿zipline

引擎最大的问题是domain如何解析与设计：
- 因为要适配日频更新场景，所以在 resolve domain 前需要知道lookback，从而知道在日历上读多少天。那这是否意味着需要提前lookback解析，或者提前整个编译？
- DomainSpec字段已经确定：start, end, assets, target_asset, target_freq

进一步的讨论：
- dotted key 明确不继续隐式表示 source
- 确定helper返回AST expr，其中使用sourceref表达source引用；字符串 parser 产生 AST
- 确定SourceRefExpr表达“公式需要什么数据”；SourceTerm表示“这个逻辑输入在 DAG 中的身份”；SourceBinding表达把 SourceTerm 绑定到本次任务的读取方式，包含SourceSpec，即“物理上去哪里读”
- 确定domain拆成OutputDomain“用户要求返回的 start/end、目标资产轴、目标频率和 step”；TermDomain“每个 Term 自己所在的资产、频率和 step 空间”；ReadDomain“PhysicalPlan 根据 lookback 扩展出来的实际读取日期”
- DomainSpec 中 assets，我的设想是：起到确定本次工作的资产范围的能力，有些时候不需要cb/idx，可以声明只有stk，即“目标资产选择”；有时只需部分idx，可以用字典比如{“idx”: ['000300', '000905']}来限制范围。
- 确定第一版做简单的“每个分区统一读取最大 read window，最终只裁剪 write dates”

对于多行公式，需要结合实际应用场景进一步讨论：
- 期望的输入是多组多行字符串公式：
- factor1：
- - avg_price = ts_mean(close, 5)
- - vol_20d = ts_std(vol, 20)
- - intermediate = avg_price / power(vol_20d, 2)
- - factor = ts_corr(intermediate, amount, 10)

factor2:
- - amihud = amihud(close, vol, 20)
- - swing = ts_max(high - close, 20)
- - volatiliy = ts_std(vol, 20)
- - intermediate = amihud / swing
- - factor = ln(volatility) + intermediate

- 在多行字符串公式定义前，会统一定义source引用：
- - close = get_lf("stk", "ClosePrice")
- - vol = get_lf("stk", "Volume")

- 可以看出，多行字符串公式场景的重点是：
1. 单个多行公式内部是完备的，不会出现比如要用到ts_std(vol, 20)，但在自己这组没有定义，借用其他公式组中定义的情况
2. 同样的一个公式如ts_std(vol, 20)，在不同公式组等号左侧的名称可能不同
3. 在不同公式组，同一个等号左侧名称对应的公式可能不同（如intermediate）
4. 可以约定每组公式最后一个赋值就是要输出的因子

更进一步的讨论：
- domainspec中的asset更名为assetscope
- ast 中 引入 SymbolRefExpr，表示“引用当前作用域内已经绑定的名称”，名称绑定阶段会把引用替换为它所指向的 AST
- 目前暂定通过load_factor("alpha_001") 像定义source一样引用已物化因子
- 确定采用一下批任务结构：
FormulaBatch
├── inputs：所有公式组共享的输入名称
└── formulas
    ├── factor1：独立的顺序绑定程序
    └── factor2：独立的顺序绑定程序
- 约定公式组可以引用公共输入和自己前面已经定义的变量，不能引用其他公式组的局部变量；每组最后一个赋值是输出；禁止前向引用

计算引擎的输出：
- 现有workspace可以直接向外输出结果数组，然而，由于numpy array 不带有信息，因此要么提供一个方法，复原为dataframe输出；要么保存下来，需要把domain中的dates和codes一并保存
- 如果涉及到保存，那也涉及到读取，因为如果要复用这些因子，需要有方法读取这些信息
- 这也相应的涉及这些输出/读取方法的边界归属问题，是应该归属计算引擎还是因子仓库？
- 目前的存储方式是沿用很早的旧项目，因此仅作参考不做任何约束，numpy array的特性确实引入不少复杂度，尤其是io方案设计，是否有python包可以解决这个问题？

关于引擎输出的进一步讨论：
- 暂时接受 Runtime - ResultChunk - ResultStream 的输出协议
- 目前暂时不足以设计出完善的FactorRepository，暂不深入讨论，请你以 计算引擎 为主导，FactorRepository 仅作为临时实现；暂不引入xarray

其他关键协议：
- 数值协议：numeric float64/NaN；mask 1.0/0.0/NaN；code float64/NaN
- OperatorSpec 明确需要 date_lookback 以便稳定推导lookback
- 单个chunk失败整个因子失效；第一版做任务级fast-fail
- SourceRefExpr“我要股票日频 ClosePrice”
      ↓ describe
  InputSpec
    asset_type=stk, frequency=1d, steps=1, value_kind=numeric
      ↓ Compiler
  SourceTerm
    DAG 中的逻辑输入节点已经拥有 InputSpec 和 TermDomain
      ↓ PhysicalPlanner 得到 ReadDomain
  SourceBinding
    term_id, source_spec, read_domain, load_group_key
      ↓ load_many
  ndarray

之后可以做的feature：
- FactorRepository 的正式存储格式。
- Zarr、Parquet 或其他 IO 后端。
- xarray 和 DataFrame 输出。
- 多进程与分布式执行。
- 自适应内存分块。
- per-Term offset。
- 公式级独立失败恢复。
- 重试、取消和任务恢复。
- 跨任务缓存。
- 通用 nullable dtype 系统。
- 研究层 Registry 和因子生命周期。
- 自动挖掘策略。
- 详细 profiling 和数值质量统计。