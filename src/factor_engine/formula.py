"""定义公式批次的语法节点、解析器与符号绑定。"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .operators import default_operator_registry


@dataclass(frozen=True)
class SourceSpan:
    """表达式或语句在公式源码中的行列位置。"""

    line: int
    column: int
    source: str

    def __str__(self) -> str:
        """返回适合错误消息展示的源码位置。"""
        return f"{self.source}:{self.line}:{self.column}"


class FormulaError(ValueError):
    """公式语言相关错误的基类，携带出错阶段标识。"""

    stage = "formula"


class FormulaParseError(FormulaError):
    """公式文本解析失败时抛出的错误。"""

    stage = "parse"


class SymbolBindingError(FormulaError):
    """符号引用绑定失败时抛出的错误。"""

    stage = "symbol_binding"


@dataclass(frozen=True)
class Expr:
    """公式表达式节点的基类，可选携带源码位置。"""

    span: SourceSpan | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class LiteralExpr(Expr):
    """常量字面量表达式。"""

    value: Any


@dataclass(frozen=True)
class SymbolRefExpr(Expr):
    """对当前作用域内已绑定名称的符号引用。"""

    name: str


@dataclass(frozen=True)
class SourceRefExpr(Expr):
    """指向逻辑数据键的数据源引用，语义参数不可变。"""

    logical_key: str
    semantic_params: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def create(cls, logical_key: str, **semantic_params: Any) -> SourceRefExpr:
        """根据逻辑键和语义参数创建不可变数据源引用。"""
        return cls(
            str(logical_key),
            tuple(
                (str(key), _freeze(value))
                for key, value in sorted(semantic_params.items())
            ),
        )

    @property
    def params(self) -> Mapping[str, Any]:
        """返回解冻后的只读语义参数映射。"""
        return MappingProxyType(
            {key: _thaw(value) for key, value in self.semantic_params}
        )


@dataclass(frozen=True)
class OperatorExpr(Expr):
    """由算子名称、输入表达式和参数组成的算子表达式。"""

    name: str
    args: tuple[Expr, ...] = ()
    params: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class HelperExpr(Expr):
    """规范化之前展开为 source 引用和算子表达式的 Helper。"""

    name: str
    args: tuple[Expr, ...] = ()
    params: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Binding:
    """公式程序中一条“名称 = 表达式”的命名绑定。"""

    name: str
    expression: Expr
    span: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FormulaProgram:
    """按声明顺序组织的绑定序列，即一个公式程序。"""

    bindings: tuple[Binding, ...] = ()


@dataclass(frozen=True)
class BoundFormulaBatch:
    """符号绑定完成后的公式批次，含公共输入与各公式输出表达式。"""

    common_inputs: Mapping[str, Expr]
    outputs: Mapping[str, Expr]


@dataclass(frozen=True, init=False)
class FormulaBatch:
    """共享公共输入和一个输出域的一组相互独立公式。"""

    common_inputs: FormulaProgram
    formulas: Mapping[str, FormulaProgram]

    def __init__(
        self,
        common_inputs: FormulaProgram,
        formulas: Mapping[str, FormulaProgram],
    ) -> None:
        """创建包含公共输入和至少一个输出公式的批次。"""
        if not formulas:
            raise ValueError("FormulaBatch requires at least one formula")
        object.__setattr__(self, "common_inputs", common_inputs)
        object.__setattr__(self, "formulas", MappingProxyType(dict(formulas)))

    @classmethod
    def from_text(
        cls,
        *,
        common_inputs: str = "",
        formulas: Mapping[str, str],
        helper_names: set[str] | None = None,
        operator_names: set[str] | None = None,
    ) -> FormulaBatch:
        """将公共输入文本和多组公式文本解析为公式批次。"""
        # 所有程序共用同一允许名称集合，保证解析规则一致。
        parser = FormulaParser(
            helper_names=helper_names,
            operator_names=operator_names,
        )
        # 逐个规范公式标识并解析其独立程序。
        parsed: dict[str, FormulaProgram] = {}
        for formula_id, text in formulas.items():
            formula_id = str(formula_id).strip()
            if not formula_id:
                raise ValueError("formula_id must not be empty")
            if formula_id in parsed:
                raise ValueError(f"Duplicate formula_id {formula_id!r}")
            parsed[formula_id] = parser.parse_program(text, source=formula_id)
        return cls(
            parser.parse_program(
                common_inputs, source="common_inputs", allow_empty=True
            ),
            parsed,
        )

    def bind(self, *, reserved_names: set[str] | None = None) -> BoundFormulaBatch:
        """按作用域绑定所有符号并返回各公式的最终表达式。"""
        # 公共输入先绑定，随后作为每个输出公式的初始环境。
        reserved = set(reserved_names or ())
        common = _bind_program(
            self.common_inputs,
            formula_id="common_inputs",
            initial={},
            reserved=reserved,
            other_formula_names=set(),
            require_output=False,
        )
        # 收集各公式局部名称，用于识别非法跨公式引用。
        all_locals = {
            formula_id: {binding.name for binding in program.bindings}
            for formula_id, program in self.formulas.items()
        }
        outputs: dict[str, Expr] = {}
        for formula_id, program in self.formulas.items():
            other_names = set().union(
                *(names for key, names in all_locals.items() if key != formula_id)
            )
            local = _bind_program(
                program,
                formula_id=formula_id,
                initial=dict(common),
                reserved=reserved,
                other_formula_names=other_names,
                require_output=True,
            )
            outputs[formula_id] = local[program.bindings[-1].name]
        return BoundFormulaBatch(
            MappingProxyType(common),
            MappingProxyType(outputs),
        )


DEFAULT_HELPERS = {
    "source",
    "get_lf",
    "get_hf",
    "get_fund",
    "load_factor",
    "select_asset",
    "select_index_feature",
    "index_member_stat",
    "project_stk_to_cb",
}

# ast支持的运算符
_BIN_OPS = {
    ast.Add: "add",
    ast.Sub: "subtract",
    ast.Mult: "multiply",
    ast.Div: "divide",
    ast.Pow: "power",
}


_CMP_OPS = {
    ast.GtE: "greater_equal",
    ast.Gt: "greater",
    ast.LtE: "less_equal",
    ast.Lt: "less",
    ast.Eq: "equal",
    ast.NotEq: "not_equal",
}


class FormulaParser:
    """把受限 Python 赋值文本解析为公式程序的解析器。"""

    def __init__(
        self,
        *,
        helper_names: set[str] | None = None,
        operator_names: set[str] | None = None,
    ) -> None:
        """使用允许的 helper 和 operator 名称初始化公式解析器。"""
        # 未显式传入时复制默认名称集合，避免共享可变对象。
        self.helper_names = set(
            DEFAULT_HELPERS if helper_names is None else helper_names
        )
        self.operator_names = set(
            default_operator_registry() if operator_names is None else operator_names
        )

    @property
    def reserved_names(self) -> set[str]:
        """返回公式中不能作为绑定名使用的保留名称。"""
        return self.helper_names | self.operator_names

    def parse_program(
        self,
        text: str,
        *,
        source: str,
        allow_empty: bool = False,
    ) -> FormulaProgram:
        """把受限 Python 赋值程序解析为带源码位置的公式程序。"""
        # 先清理缩进并交给 Python AST 做基础语法解析。
        raw = textwrap.dedent(str(text)).strip()
        if not raw:
            if allow_empty:
                return FormulaProgram()
            raise FormulaParseError(f"{source}: formula program is empty")
        try:
            tree = ast.parse(raw, mode="exec")
        except SyntaxError as exc:
            location = f"{source}:{exc.lineno or 1}:{exc.offset or 1}"
            raise FormulaParseError(f"{location}: {exc.msg}") from exc

        # 程序仅允许不重复的简单名称赋值。
        bindings: list[Binding] = []
        seen: set[str] = set()
        for statement in tree.body:
            span = self._span(statement, source)
            if (
                not isinstance(statement, ast.Assign)
                or len(statement.targets) != 1
                or not isinstance(statement.targets[0], ast.Name)
            ):
                raise FormulaParseError(
                    f"{span}: only simple 'name = expression' bindings are allowed"
                )
            name = statement.targets[0].id
            if name in seen:
                raise FormulaParseError(f"{span}: duplicate binding {name!r}")
            if name in self.reserved_names:
                raise FormulaParseError(f"{span}: binding name {name!r} is reserved")
            seen.add(name)
            bindings.append(Binding(name, self._convert(statement.value, source), span))
        return FormulaProgram(tuple(bindings))

    def _convert(self, node: ast.AST, source: str) -> Expr:
        """把受支持的 Python AST 节点转换为公式表达式节点。"""
        # 名称和基础字面量直接映射为叶子表达式。
        span = self._span(node, source)
        if isinstance(node, ast.Name):
            return SymbolRefExpr(node.id, span=span)
        if isinstance(node, ast.Constant):
            return LiteralExpr(node.value, span=span)
        if isinstance(node, (ast.List, ast.Tuple, ast.Dict)):
            try:
                return LiteralExpr(ast.literal_eval(node), span=span)
            except (TypeError, ValueError) as exc:
                raise FormulaParseError(f"{span}: invalid literal") from exc
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return OperatorExpr(
                "neg", (self._convert(node.operand, source),), span=span
            )
        if isinstance(node, ast.BinOp):
            # Python 二元运算符映射到引擎的具名算子。
            name = _BIN_OPS.get(type(node.op))
            if name is None:
                raise FormulaParseError(f"{span}: unsupported binary operator")
            return OperatorExpr(
                name,
                (self._convert(node.left, source), self._convert(node.right, source)),
                span=span,
            )
        if isinstance(node, ast.Compare):
            # 比较表达式限制为单次比较，避免链式语义歧义。
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise FormulaParseError(
                    f"{span}: chained comparisons are not supported"
                )
            name = _CMP_OPS.get(type(node.ops[0]))
            if name is None:
                raise FormulaParseError(f"{span}: unsupported comparison operator")
            return OperatorExpr(
                name,
                (
                    self._convert(node.left, source),
                    self._convert(node.comparators[0], source),
                ),
                span=span,
            )
        if isinstance(node, ast.Call):
            # helper 关键字仍是字面量；operator 可额外接收具名 Term 输入。
            if not isinstance(node.func, ast.Name):
                raise FormulaParseError(
                    f"{span}: only simple function calls are allowed"
                )
            params: list[tuple[str, Any]] = []
            is_helper = node.func.id in self.helper_names
            for keyword in node.keywords:
                if keyword.arg is None:
                    raise FormulaParseError(f"{span}: **kwargs are not supported")
                try:
                    value = ast.literal_eval(keyword.value)
                except (TypeError, ValueError) as exc:
                    if is_helper:
                        raise FormulaParseError(
                            f"{self._span(keyword.value, source)}: keyword arguments must be literals"
                        ) from exc
                    value = self._convert(keyword.value, source)
                params.append((keyword.arg, value))
            args = tuple(self._convert(arg, source) for arg in node.args)
            cls = HelperExpr if is_helper else OperatorExpr
            return cls(node.func.id, args, tuple(params), span=span)
        raise FormulaParseError(f"{span}: unsupported formula syntax")

    @staticmethod
    def _span(node: ast.AST, source: str) -> SourceSpan:
        """提取 AST 节点在指定源码中的行列位置。"""
        return SourceSpan(
            int(getattr(node, "lineno", 1)),
            int(getattr(node, "col_offset", 0)) + 1,
            source,
        )


def _bind_program(
    program: FormulaProgram,
    *,
    formula_id: str,
    initial: dict[str, Expr],
    reserved: set[str],
    other_formula_names: set[str],
    require_output: bool,
) -> dict[str, Expr]:
    """按声明顺序绑定单个程序并返回更新后的名称环境。"""
    # 绑定严格遵循声明顺序，并阻止覆盖公共输入或保留名称。
    if require_output and not program.bindings:
        raise SymbolBindingError(f"{formula_id}: formula program is empty")
    environment = initial
    local_names: set[str] = set()
    for binding in program.bindings:
        if binding.name in reserved:
            raise SymbolBindingError(
                f"{binding.span or formula_id}: binding name {binding.name!r} is reserved"
            )
        if binding.name in initial:
            raise SymbolBindingError(
                f"{binding.span or formula_id}: local binding {binding.name!r} shadows a common input"
            )
        if binding.name in local_names:
            raise SymbolBindingError(
                f"{binding.span or formula_id}: duplicate binding {binding.name!r}"
            )
        # 解析当前表达式时保留未来名称集合以报告前向引用。
        future = {item.name for item in program.bindings} - local_names
        environment[binding.name] = _resolve_symbols(
            binding.expression,
            environment,
            formula_id=formula_id,
            future_names=future,
            other_formula_names=other_formula_names,
        )
        local_names.add(binding.name)
    return environment


def _resolve_symbols(
    expr: Expr,
    environment: Mapping[str, Expr],
    *,
    formula_id: str,
    future_names: set[str],
    other_formula_names: set[str],
) -> Expr:
    """递归解析表达式中的符号引用并报告非法作用域访问。"""
    # 符号叶子从当前环境替换，并区分三类非法引用原因。
    if isinstance(expr, SymbolRefExpr):
        if expr.name in environment:
            return environment[expr.name]
        if expr.name in future_names:
            reason = "forward reference"
        elif expr.name in other_formula_names:
            reason = "cross-formula reference"
        else:
            reason = "unknown name"
        raise SymbolBindingError(
            f"{expr.span or formula_id}: {reason} {expr.name!r} in formula {formula_id!r}"
        )
    # 运算符与 helper 保持节点类型，只递归替换其参数。
    if isinstance(expr, OperatorExpr):
        return OperatorExpr(
            expr.name,
            tuple(
                _resolve_symbols(
                    arg,
                    environment,
                    formula_id=formula_id,
                    future_names=future_names,
                    other_formula_names=other_formula_names,
                )
                for arg in expr.args
            ),
            tuple(
                (
                    name,
                    (
                        _resolve_symbols(
                            value,
                            environment,
                            formula_id=formula_id,
                            future_names=future_names,
                            other_formula_names=other_formula_names,
                        )
                        if isinstance(value, Expr)
                        else value
                    ),
                )
                for name, value in expr.params
            ),
            span=expr.span,
        )
    if isinstance(expr, HelperExpr):
        return HelperExpr(
            expr.name,
            tuple(
                _resolve_symbols(
                    arg,
                    environment,
                    formula_id=formula_id,
                    future_names=future_names,
                    other_formula_names=other_formula_names,
                )
                for arg in expr.args
            ),
            expr.params,
            span=expr.span,
        )
    return expr


def source(logical_key: str, **semantic_params: Any) -> SourceRefExpr:
    """构造指向任意逻辑数据键的数据源引用。"""
    return SourceRefExpr.create(logical_key, **semantic_params)


def get_lf(asset: str, feature: str, **semantic_params: Any) -> SourceRefExpr:
    """构造指定资产日频字段的数据源引用。"""
    return source(f"{asset}.1d.{feature}", **semantic_params)


def get_hf(
    asset: str,
    frequency: str,
    feature: str,
    *,
    resample: str | None = None,
    method: str | None = None,
    **semantic_params: Any,
) -> Expr:
    """构造高频 Source，并可显式包装为重采样表达式。"""
    raw = source(f"{asset}.{frequency}.{feature}", **semantic_params)
    if resample is None:
        if method is not None:
            raise ValueError("get_hf() method requires resample")
        return raw
    if method is None:
        raise ValueError("get_hf() resample requires an explicit method")
    return operator(
        "resample", raw, target_freq=str(resample), method=str(method)
    )


def get_fund(asset: str, feature: str, **semantic_params: Any) -> SourceRefExpr:
    """构造指定资产基本面字段的数据源引用。"""
    return source(f"{asset}.1d.{feature}", **semantic_params)


def load_factor(factor_id: str) -> SourceRefExpr:
    """构造指向已保存因子的逻辑数据源引用。"""
    return source(f"factor:{factor_id}")


def operator(name: str, *args: Expr | Any, **params: Any) -> OperatorExpr:
    """根据名称、输入和配置参数直接构造运算符表达式。"""
    expressions = tuple(
        value if isinstance(value, Expr) else LiteralExpr(value) for value in args
    )
    return OperatorExpr(
        str(name),
        expressions,
        tuple((str(key), _freeze(value)) for key, value in sorted(params.items())),
    )


def _freeze(value: Any) -> Any:
    """递归把容器值转换为可哈希的不可变形式。"""
    # 列表和元组冻结为元组，映射按键排序后冻结为键值对元组。
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (str(key), _freeze(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    return value


def _thaw(value: Any) -> Any:
    """递归恢复冻结后的语义参数容器。"""
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {key: _thaw(item) for key, item in value}
        return tuple(_thaw(item) for item in value)
    return value


def parse_formula_batch(text: str) -> FormulaBatch:
    """把每行一个的“公式标识 = 表达式”文本转换为公式批次。"""
    # 忽略空行和注释，并把每个右值包装成标准输出绑定。
    formulas: dict[str, str] = {}
    for line_number, raw_line in enumerate(str(text).splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Line {line_number} must be 'formula_id = expression'")
        formula_id, expression = (part.strip() for part in line.split("=", 1))
        if formula_id in formulas:
            raise ValueError(f"Duplicate formula_id {formula_id!r}")
        formulas[formula_id] = f"output = {expression}"
    return FormulaBatch.from_text(formulas=formulas)
