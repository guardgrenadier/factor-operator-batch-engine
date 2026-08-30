1. FormulaBatch.from_text(): python中三引号是什么意思？日后可能inputs不希望用字符串 --Python 中的三引号 """...""" 或 '''...''' 表示多行字符串，是 Python 自身的语法
2. 由于helper是函数，要利用helper的复权停牌等参数，要写：
- close = get_lf() 这样要支持
3. 传入 FormulaParser 的 helper_names 和 operator_names 的作用是什么？ --识别helper，operator_names 保留名称
4. sourcespan的作用是什么 --定位ast解析结果中的位置，用于报错信息打印
5. 目前ExecutionOptions中是不是缺少如mask这样的参数，我想实现的其实是任务级input_mask/output_mask
6. 目前 _expand_helpers() 方法识别load_factor("alpha_1")时，生成SourceRefExpr.create("factor:alpha_1")，通过 factor: 前缀 和后续 RepositoryDataProvider 约定
7. project_stk_to_cb helper似乎有偏差；还没实现行业统计helper
8. 需理清helper，formula的全生命周期流程
9. 需理清helper-sourcerefexpr-inputspec-sourcespec的source全生命周期
10. 目前还没实现最终的 dataprovider，目前provider实质复用之前store的能力；且目前采用全读日历然后截取start/end；需要确定交易日lookback如何实现
11. 目前Compiler._asset_axes 同样从store的codes中取
12. compiler._lower_source() 中是否把 codes 复制多次？编译期间似乎做了太多次dates/codes/freq/steps检查，项目设计是通过DomainSpec统一一次任务的dates和codes，这样内部检查是否必要？
13. 内部校验较为复杂，且和预期的资产间引用方式可能不符；需要把资产对齐降低到operator级的能力，比如在算转债因子的时候，可能需要在股票上先算一个特征，因此可能局部完全是股票轴，这样似乎只要确保同一个算子内部统一资产，不同情况下优先往target_asset转；look_up_by_col保持例外
14. termdomain是否过重，本质上资产，频率的校验仅仅涉及 asset 和 step(freq) 两个参数，而不需要完整codes/dates，唯一需要的情况是如果要做 zipline 式的更精细 lookback 读取，需要更详细的 dates 信息
15. 目前可以接受，但资产引用频率对齐规则仍略微分散/耦合；以及是否能这样理解：目前在编译阶段为每个term绑定domain，实际上是为了未来实现 zipline 的term级read_domain做铺垫？
16. 目前 PhysicalPlanner 也是在 dates 上切片形成分块
17. 各种domain类和execution_scope是否有多余？好像不一定，因为涉及不同资产类别，比如重新构造 ReadDomain
18. 是不是目前没实现真正 load_many ？似乎router还是在group中逐个读取？
19. runtime中这种操作：args = [workspace[input_id] for input_id in term.input_term_ids] 会复制数组吗？
20. runtime中 workspace.pop(input_id, None) 是否能正确释放内存
21. 总体上校验过多