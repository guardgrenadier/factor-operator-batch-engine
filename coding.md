## 我应该拥有：

- 问题定义
- 架构
- 核心数据模型
- 模块 contract
- 关键数据流
- 系统 invariant
- 风险判断
- 关键 edge case

## coding-with-agent

    1 Intent
    ↓
    2 Architecture check
    ↓
    3 Change contract
    ↓
    4 Implementation
    ↓
    5 Verification
    ↓
    6 Structural review
    ↓
    7 Merge / Learn

### 1. 分类任务

| 类型          | 例子                               | 你的投入 |
| ----------- | -------------------------------- | ---- |
| **T1 局部实现** | 小 bug、小功能、普通 adapter             | 很低   |
| **T2 结构影响** | 新数据模型、新 abstraction、跨模块 contract | 中高   |
| **T3 核心语义** | 计算正确性、PIT、交易逻辑、核心引擎              | 很高   |

- 给 T1 加一个“Complexity Fuse” :
- 如果 Agent 开始探索后发现必须：加一个新模型/修改多个核心模块/加 compatibility layer/在通用代码中增加特殊分支/改变已有 contract/产生远超预期行数的 diff，那么不允许继续写，必须升级任务。

- 类似：

    This task is expected to be a local change.

    If investigation shows that a correct fix requires:
    - a new persistent abstraction or data model,
    - a new special case in a generic path,
    - a compatibility/fallback mechanism,
    - changing a cross-module contract,
    - or substantially larger changes than expected,

    stop before implementation.

    Explain:
    1. why the task expanded,
    2. whether this reveals an abstraction problem,
    3. the smallest patch option,
    4. the structural solution.


### 2. Architecture Check

- 先问“现有设计能不能自然表达它？” ：

    - 这个需求在现有架构里属于哪里？
    - 现有 abstraction 是否自然支持？
    - 需要增加新的例外吗？
    - 现有项目有没有 canonical pattern？

    - 这是：
    A. 正常实例
    B. 真正新的领域概念
    C. 暴露了现有 abstraction 缺陷

- planning 阶段要克制，太长同样无法review

### 3. Change Contract & Implementation & Verification

    Goal
    修改什么行为？

    Design fit
    它为什么属于当前 abstraction？

    Affected contracts
    哪些 module/data contract 改变？

    Exceptions
    是否增加特殊情况？

    Verification
    什么事实证明它完成了？

    
- Implementation -- ''Enforce invariants, not implementations.''
- 明确 invariants 与边界，具体执行信任 agent

### 4. Structural review -- 人理解 Contract/Dataflow 层 Agent 掌控代码层

- 项目具备三层 mental model

    - Layer 1：Architecture 
        - 有哪些主要模块？
        - 每个模块负责什么？
        - 依赖方向是什么？

    - Layer 2：Contracts
        - 模块 A 给模块 B 什么？
        - B 可以假设什么？
        - 哪里负责 validation？
        - 谁拥有状态？

    - Layer 3：Critical flows
        - 对核心系统，只维护少量真实路径，每个箭头都要知道是什么数据：

            外部调用
            → API
            → service
            → engine
            → storage
            → result

### 5. Documentation

    维护 “最小知识内核”

    AGENTS.md
        ≈ 项目地图 + 如何工作

    ARCHITECTURE.md
        ≈ 分层 / 模块 / 依赖方向

    INVARIANTS.md
        ≈ 极少数长期不能违反的原则

    GOLDEN_PATHS.md
        ≈ 项目里真正推荐模仿的 canonical patterns

    重大设计才写 ADR

### 6. tests/

测试系统应该分三层

    第一层：Implementation tests
    - AI 自己写。
    作用：
    - 这次实现有没有明显坏。

    第二层：Invariant / contract tests
    Human 决定测试什么性质：
        - 同样输入必须确定性一致
        - 转换不能改变某个 identity
        - 重复执行应该幂等
        - 错误数据必须在 boundary 被拒绝
        - 不允许跨 layer dependency

    Agent 写 test implementation。

    第三层：高风险系统的 adversarial / property / mutation tests

    **property-based testing**
    - agent 先推断应该始终成立的 property，再用 Hypothesis 大量构造输入，而不是只测试几个 example。

    **Mutation testing**
    - 如果我故意把代码稍微写错，tests 能抓住吗？

测试 Agent 与实现 Agent 分离

方案一：TDD

    test-writer agent
    ↓
    coder agent

方案二：planner -> generator -> evaluator


### 7. Review
区分三种Review：

1. Review A：Behavior
- AI 承担

    - 需求实现了吗？
    - 测试了吗？
    - edge case？

2. Review B：Structure
- 独立 fresh agent 完成
    - 是否新增 unnecessary abstraction？
    - 是否新增 special case？
    - 是否重复已有概念？
    - 是否引入 hidden contract？
    - 是否让 dependency direction 变差？
    - 是否出现 compatibility/fallback？
    - 是否留下 dead code？

3. Review C：Human architecture review
- 我只对 T2/T3 投入，看：
    - 这个 change 在系统中到底属于哪里？
    - 数据模型是否仍然正确？
    - 重要 invariant 是否被改变？
    - 以后修改这里还会不会容易？

### 8. Learn & Cleanup
Agent 犯错的升级路径：

    第一次：
    人工发现

    第二次：
    写 invariant / golden principle

    第三次：
    机械 enforcement

定期做/复杂度触发 Cleanup ：

    一个 generic method 出现第三个 special case
    同一概念出现第二套模型
    一个小 change 突然跨 4 个模块
    需要写 compatibility layer 才能完成普通需求
    核心 module 已经无法一屏解释 responsibility
    Agent reviewer 多次指出同类 structure smell

扫描并产生非常小的 cleanup changes：

    dead code
    duplicate abstraction
    special-case growth
    oversized modules
    dependency violations
    stale compatibility
    stale docs

### 9. Sessions

    project memory = repo
    session memory = disposable

维护一个主 session ，专门进行 architecture exploration