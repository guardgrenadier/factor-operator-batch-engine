# 代码阅读理解

本文用于记录项目 review 过程中对当前代码实现的理解。

## `FormulaBatch.bind()`

### 核心作用

`bind()` 根据作用域规则，把公共输入和局部变量的符号引用替换为它们绑定的表达式，同时将每个公式的中间变量递归内联，最终得到每个因子的完整输出表达式。

这里的“绑定”是程序名称到 AST 表达式的绑定，不是物理数据源绑定。字符串公式中的 `source(...)` 在这一阶段仍可能是 `HelperExpr`；它会在后续 `Compiler._expand_helpers()` 中转换为 `SourceRefExpr`，物理 `SourceSpec` 则要到 Runtime 调用 `DataProvider.bind_many()` 时才产生。

### 现行作用域规则

- `common_inputs` 按声明顺序绑定，对所有公式可见。
- 每个公式拥有独立的局部作用域，可以引用公共输入和本公式中此前定义的局部名称。
- 每个公式的最后一个 binding 是该 `formula_id` 的输出。
- 禁止前向引用，即不能引用当前程序中尚未完成绑定的名称。
- 禁止跨公式引用其他公式的局部名称。
- 禁止局部 binding 覆盖公共输入。
- 禁止同一程序内重复定义名称。
- helper 和 operator 名称属于保留名称，不能作为 binding 名称。
- binding 名称只服务于作用域和表达式组织，不进入最终 Term DAG 的结构身份。因此，命名不同但表达式结构相同的公式仍可在后续编译中进行 CSE。

### 实现调用链

```text
Compiler.compile(request)
  -> FormulaBatch.bind(reserved_names=operators + helpers)
     -> _bind_program(common_inputs, initial={})
        -> 按声明顺序处理公共输入
        -> _resolve_symbols(expression, environment, ...)
        -> 建立公共名称到已解析表达式的映射 common
     -> 收集每个公式声明的局部名称 all_locals
        -> 用于区分 cross-formula reference 和 unknown name
     -> 对每个公式调用 _bind_program(initial=dict(common))
        -> 从公共输入环境的副本开始
        -> 校验保留名称、名称覆盖和重复定义
        -> 计算尚未绑定的 future_names
        -> _resolve_symbols() 递归遍历 OperatorExpr/HelperExpr
        -> 将 SymbolRefExpr 替换为 environment 中对应的完整表达式
        -> 把已解析的局部表达式加入 environment
     -> 取每个公式最后一个 binding 对应的表达式
     -> 返回 BoundFormulaBatch(common_inputs=common, outputs=outputs)
```

其中，`_bind_program()` 负责按顺序建立和更新当前名称环境；`_resolve_symbols()` 负责递归替换表达式中的 `SymbolRefExpr`，并将非法引用区分为前向引用、跨公式引用和未知名称。

例如：

```python
avg = ts_mean(close, 5)
ratio = avg / volume
factor = rank(ratio)
```

完成 bind 后，`factor` 在概念上成为：

```python
rank(ts_mean(close绑定的表达式, 5) / volume绑定的表达式)
```

后续 Compiler 再负责 helper 展开、Domain lowering，以及将完整表达式降低为可执行的共享 Term DAG。
