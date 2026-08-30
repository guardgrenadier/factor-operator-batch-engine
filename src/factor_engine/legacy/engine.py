"""旧版实现：公式解析、表达式规划与执行的核心引擎。"""

from __future__ import annotations

import ast
import re
import textwrap
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..operators import OperatorSpec, default_operator_registry
from .data.model import (
    CalculationResult,
    ExecutionScope,
    FeatureArray,
    FeatureDef,
    FeatureSpace,
    SourceSpec,
    get_ffill_step_index,
    is_intraday_freq,
    parse_intraday_minutes,
    parse_feature_key,
)
from .data.sources import INDEX_INNER_CODES
from .data.store import FeatureStore


class Expr:
    """旧版表达式树的基类。"""

    pass


@dataclass(frozen=True)
class ConstExpr(Expr):
    """携带字面量值的常量表达式。"""

    value: Any


@dataclass(frozen=True)
class FeatureExpr(Expr):
    """引用已注册特征键的叶子表达式。"""

    key: str
    alias: str | None = None


@dataclass(frozen=True)
class SourceExpr(Expr):
    """携带显式读取参数的外部数据源表达式。"""

    spec: SourceSpec

    @property
    def key(self) -> str:
        """返回显式外部输入的参数化逻辑键。"""
        return self.spec.key


@dataclass(frozen=True)
class AliasExpr(Expr):
    """待解析别名的符号引用表达式。"""

    name: str


@dataclass(frozen=True)
class OpExpr(Expr):
    """调用具名算子的复合表达式。"""

    op: str
    args: tuple[Expr, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BroadcastIndexFeatureExpr(Expr):
    """按指数选择并广播到目标资产空间的 helper 表达式。"""

    index: str
    feature: str
    freq: str = "1d"


@dataclass(frozen=True)
class RuntimeFeatureExpr(Expr):
    """引用本次执行会话内已计算特征的表达式。"""

    key: str


_BIN_OPS = {
    ast.Add: "add",
    ast.Sub: "subtract",
    ast.Mult: "multiply",
    ast.Div: "divide",
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
    """把公式字符串解析为旧版表达式树的解析器。"""

    def parse(self, formula: str) -> Expr:
        """把公式字符串解析为表达式树。"""
        formula = textwrap.dedent(str(formula)).strip()
        formula, raw_refs = _rewrite_dotted_refs(formula)
        self._raw_refs = raw_refs
        tree = ast.parse(formula, mode="eval")
        return _expand_formula_helpers(self._convert(tree.body))

    def _convert(self, node: ast.AST) -> Expr:
        """把 Python AST 节点转换为内部 Expr 节点。"""
        # 名称优先还原为完整特征键，否则保留为待解析别名。
        if isinstance(node, ast.Name):
            raw_key = getattr(self, "_raw_refs", {}).get(node.id)
            if raw_key is not None:
                return FeatureExpr(raw_key)
            return AliasExpr(node.id)
        if isinstance(node, ast.Constant):
            return ConstExpr(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return OpExpr("neg", (self._convert(node.operand),), {})
        # 二元运算与单次比较映射为注册表中的具名算子。
        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported binary operator {ast.dump(node.op)}")
            return OpExpr(op, (self._convert(node.left), self._convert(node.right)), {})
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("Chained comparisons are not supported")
            op = _CMP_OPS.get(type(node.ops[0]))
            if op is None:
                raise ValueError(
                    f"Unsupported comparison operator {ast.dump(node.ops[0])}"
                )
            return OpExpr(
                op, (self._convert(node.left), self._convert(node.comparators[0])), {}
            )
        # 函数调用只允许简单名称，关键字配置必须是字面量。
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only simple function calls are supported")
            args = tuple(self._convert(arg) for arg in node.args)
            kwargs = {kw.arg: self._literal_kwarg(kw.value) for kw in node.keywords}
            return OpExpr(node.func.id, args, kwargs)
        raise ValueError(f"Unsupported formula syntax: {ast.dump(node)}")

    def _literal_kwarg(self, node: ast.AST) -> Any:
        """解析函数调用中的字面量关键字参数。"""
        try:
            return ast.literal_eval(node)
        except Exception as exc:
            raise ValueError("Only literal keyword arguments are supported") from exc


_DOTTED_REF_RE = re.compile(
    r"(?<![\w.])([A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_]+\.[A-Za-z0-9_.]+)"
)


def _rewrite_dotted_refs(formula: str) -> tuple[str, dict[str, str]]:
    """把 dotted feature key 临时改写成合法 Python 标识符。"""
    # 临时 token 与原始完整键建立一一映射。
    raw_refs: dict[str, str] = {}
    counter = 0

    def repl(match: re.Match[str]) -> str:
        """记录一个 dotted key 并返回对应临时 token。"""
        nonlocal counter
        key = match.group(1)
        token = f"__raw_ref_{counter}"
        counter += 1
        raw_refs[token] = key
        return token

    # 只改写字符串字面量之外的文本片段。
    rewritten_parts: list[str] = []
    start = 0
    for literal_start, literal_end in _string_literal_spans(formula):
        rewritten_parts.append(_DOTTED_REF_RE.sub(repl, formula[start:literal_start]))
        rewritten_parts.append(formula[literal_start:literal_end])
        start = literal_end
    rewritten_parts.append(_DOTTED_REF_RE.sub(repl, formula[start:]))
    return "".join(rewritten_parts), raw_refs


def _string_literal_spans(text: str) -> list[tuple[int, int]]:
    """找出公式中的字符串字面量区间，避免误改写。"""
    # 顺序扫描单引号、双引号及三引号字面量。
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char not in {"'", '"'}:
            i += 1
            continue
        quote = char
        triple = text.startswith(quote * 3, i)
        end_quote = quote * 3 if triple else quote
        j = i + len(end_quote)
        # 普通字符串处理反斜杠转义，三引号则直接寻找结束标记。
        while j < len(text):
            if text.startswith(end_quote, j):
                spans.append((i, j + len(end_quote)))
                i = j + len(end_quote)
                break
            if not triple and text[j] == "\\":
                j += 2
            else:
                j += 1
        else:
            spans.append((i, len(text)))
            break
    return spans


_MISSING = object()


def _expand_formula_helpers(expr: Expr) -> Expr:
    """把公式字符串中的领域 helper 调用展开为内部 Expr。"""
    # helper 只可能出现在操作节点，参数先递归展开。
    if not isinstance(expr, OpExpr):
        return expr
    args = tuple(_expand_formula_helpers(arg) for arg in expr.args)
    kwargs = dict(expr.kwargs)
    if expr.op == "source":
        return _expand_source(args, kwargs)
    if expr.op == "broadcast_index_feature":
        return _expand_broadcast_index_feature(args, kwargs)
    if expr.op == "index_member_stat":
        return _call_formula_helper(expr.op, index_member_stat, *args, **kwargs)
    if expr.op == "industry_stat":
        return _call_formula_helper(expr.op, industry_stat, *args, **kwargs)
    if expr.op == "industry_mean":
        return _call_formula_helper(expr.op, industry_mean, *args, **kwargs)
    if expr.op == "industry_demean":
        return _call_formula_helper(expr.op, industry_demean, *args, **kwargs)
    if expr.op == "industry_zscore":
        return _call_formula_helper(expr.op, industry_zscore, *args, **kwargs)
    return OpExpr(expr.op, args, _expand_keyword_feature_refs(kwargs))


def _expand_source(args: tuple[Expr, ...], kwargs: dict[str, Any]) -> SourceExpr:
    """将 source 调用展开为携带完整读取参数的外部数据源表达式。"""
    # 读取并校验 helper 支持的位置参数和关键字参数。
    key = _literal_helper_arg("source", args, kwargs, "key", 0)
    if len(args) > 1:
        raise ValueError("source accepts exactly one positional argument")
    allowed = {"source", "table", "field", "params"}
    unexpected = sorted(set(kwargs) - allowed)
    if unexpected:
        raise ValueError(
            f"source got unexpected keyword argument(s): {', '.join(unexpected)}"
        )
    params = kwargs.pop("params", None)
    if params is not None and not isinstance(params, dict):
        raise ValueError("source params must be a literal dict")
    return SourceExpr(
        SourceSpec.from_key(
            str(key),
            source=kwargs.pop("source", None),
            table=kwargs.pop("table", None),
            field=kwargs.pop("field", None),
            params=params,
        )
    )


def _expand_keyword_feature_refs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """把 keyword 参数中的完整 feature key 字符串转为 FeatureExpr。"""
    # 仅完整特征键字符串转换为表达式，普通业务字符串保持原值。
    expanded: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, str):
            try:
                expanded[key] = FeatureExpr(parse_feature_key(value).key)
                continue
            except ValueError:
                pass
        expanded[key] = value
    return expanded


def _expand_broadcast_index_feature(
    args: tuple[Expr, ...], kwargs: dict[str, Any]
) -> BroadcastIndexFeatureExpr:
    """展开公式字符串里的 broadcast_index_feature helper。"""
    # 依次读取指数、字段和可选频率三个字面量参数。
    index = _literal_helper_arg("broadcast_index_feature", args, kwargs, "index", 0)
    feature = _literal_helper_arg("broadcast_index_feature", args, kwargs, "feature", 1)
    freq = _literal_helper_arg(
        "broadcast_index_feature", args, kwargs, "freq", 2, default="1d"
    )
    if len(args) > 3:
        raise ValueError(
            "broadcast_index_feature accepts at most three positional arguments"
        )
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise ValueError(
            f"broadcast_index_feature got unexpected keyword argument(s): {unexpected}"
        )
    return broadcast_index_feature(
        index=str(index), feature=str(feature), freq=str(freq)
    )


def _literal_helper_arg(
    helper: str,
    args: tuple[Expr, ...],
    kwargs: dict[str, Any],
    name: str,
    position: int,
    *,
    default: Any = _MISSING,
) -> Any:
    """读取 helper 的字面量参数，支持位置参数和关键字参数。"""
    # 同一参数不得同时按位置和关键字提供。
    if name in kwargs:
        if len(args) > position:
            raise ValueError(f"{helper} got multiple values for argument {name!r}")
        return kwargs.pop(name)
    if len(args) > position:
        value = args[position]
        if not isinstance(value, ConstExpr):
            raise ValueError(f"{helper} argument {name!r} must be a literal")
        return value.value
    if default is not _MISSING:
        return default
    raise ValueError(f"{helper} missing required argument {name!r}")


def _call_formula_helper(helper: str, func: Any, *args: Expr, **kwargs: Any) -> Expr:
    """调用 Python helper，并把参数错误统一为公式解析错误。"""
    try:
        return func(*args, **kwargs)
    except TypeError as exc:
        raise ValueError(f"Invalid arguments for formula helper {helper!r}") from exc


SAMPLE_AWARE_OPS = {
    "cs_mean": 1,
    "cs_zscore": 1,
    "rank": 1,
    "winsorize": 1,
    "neutralize": 2,
    "group_mean": 2,
    "group_sum": 2,
    "group_std": 2,
    "group_demean": 2,
    "group_zscore": 2,
    "member_mean": 2,
    "member_sum": 2,
    "member_std": 2,
    "member_demean": 2,
    "member_zscore": 2,
}

DAILY_FROM_INTRADAY_OPS = {
    "step_mean",
    "step_sum",
    "step_last",
    "step_kurtosis",
}


@dataclass
class EvalResult:
    """执行器求值单个表达式节点的统一结果。"""

    values: Any
    asset: str | None = None
    freq: str | None = None
    space: FeatureSpace | None = None
    key: str | None = None
    missing_value: Any = np.nan


class Planner:
    """把表达式改写对齐到目标资产、频率与掩码的旧版规划器。"""

    def __init__(
        self,
        store: FeatureStore,
        *,
        aliases: dict[str, Expr],
        runtime_features: dict[str, FeatureArray | CalculationResult] | None = None,
        delay_lf: int = 1,
        delay_dict: dict[str, int] | None = None,
    ):
        """初始化表达式规划器。"""
        # 保存执行上下文，并把延迟配置键预先规范化。
        self.store = store
        self.aliases = aliases
        self.runtime_features = runtime_features if runtime_features is not None else {}
        self.delay_lf = int(delay_lf)
        self.delay_dict = {
            self._delay_key(key): int(value)
            for key, value in (delay_dict or {}).items()
        }
        self.sample_mask_injections = 0

    def _delay_key(self, key_or_alias: str) -> str:
        """把叶子表达式转换为 delay_dict 使用的完整键。"""
        try:
            return parse_feature_key(key_or_alias).key
        except ValueError:
            value = self.aliases.get(key_or_alias)
            if isinstance(value, (FeatureExpr, RuntimeFeatureExpr)):
                return parse_feature_key(value.key).key
            raise KeyError(
                f"delay_dict key {key_or_alias!r} must be a full key or a leaf alias"
            )

    def plan(
        self,
        expr: Expr,
        *,
        output_key: str,
        input_mask: str | Expr | None = None,
        sample_mask: str | Expr | None = None,
        output_mask: str | Expr | None = None,
    ) -> Expr:
        """对表达式做 alias、资产频率和 mask 规划改写。"""
        # 先冻结别名，再按输出键改写叶子的资产和频率。
        fk = parse_feature_key(output_key)
        planned = self._resolve_aliases(expr)
        planned = self._rewrite(planned, target_asset=fk.asset, target_freq=fk.freq)
        # 三类掩码分别下推到输入、注入样本算子或应用于最终输出。
        if input_mask is not None:
            mask_expr = self._rewrite(
                _mask_expr(input_mask), target_asset=fk.asset, target_freq=fk.freq
            )
            planned = self._apply_input_mask(planned, mask_expr)
        if sample_mask is not None:
            mask_expr = self._rewrite(
                _mask_expr(sample_mask), target_asset=fk.asset, target_freq=fk.freq
            )
            planned = self._inject_sample_mask(planned, mask_expr)
        if output_mask is not None:
            mask_expr = self._rewrite(
                _mask_expr(output_mask), target_asset=fk.asset, target_freq=fk.freq
            )
            planned = OpExpr("apply_mask", (planned, mask_expr), {})
        return planned

    def _resolve_aliases(self, expr: Expr) -> Expr:
        """递归把 alias 替换为实际表达式。"""
        # 别名必须已注册，算子则递归处理位置参数和关键字参数。
        if isinstance(expr, AliasExpr):
            if expr.name not in self.aliases:
                raise KeyError(f"Alias {expr.name!r} is not registered")
            return self._resolve_aliases(self.aliases[expr.name])
        if isinstance(expr, OpExpr):
            return OpExpr(
                expr.op,
                tuple(self._resolve_aliases(arg) for arg in expr.args),
                {
                    key: self._resolve_alias_value(value)
                    for key, value in expr.kwargs.items()
                },
            )
        return expr

    def _resolve_alias_value(self, value: Any) -> Any:
        """递归解析参数值中的别名表达式。"""
        if isinstance(value, Expr):
            return self._resolve_aliases(value)
        return value

    def _rewrite(self, expr: Expr, *, target_asset: str, target_freq: str) -> Expr:
        """递归改写表达式以匹配目标资产和频率。"""
        # 各类叶子交由统一特征节点规则处理。
        if isinstance(expr, FeatureExpr):
            return self._rewrite_feature(
                expr, target_asset=target_asset, target_freq=target_freq
            )
        if isinstance(expr, SourceExpr):
            return self._rewrite_feature_node(
                expr, expr.key, target_asset=target_asset, target_freq=target_freq
            )
        if isinstance(expr, RuntimeFeatureExpr):
            return self._rewrite_feature_node(
                expr, expr.key, target_asset=target_asset, target_freq=target_freq
            )
        if isinstance(expr, BroadcastIndexFeatureExpr):
            return self._rewrite_broadcast_index(
                expr, target_asset=target_asset, target_freq=target_freq
            )
        # 日内聚合使用专用规划路径。
        if isinstance(expr, OpExpr):
            if target_freq == "1d" and expr.op in DAILY_FROM_INTRADAY_OPS:
                return OpExpr(
                    expr.op,
                    tuple(
                        self._rewrite_intraday_source_arg(
                            arg, target_asset=target_asset
                        )
                        for arg in expr.args
                    ),
                    {
                        key: self._rewrite_intraday_source_value(
                            value, target_asset=target_asset
                        )
                        for key, value in expr.kwargs.items()
                    },
                )
            return OpExpr(
                expr.op,
                tuple(
                    self._rewrite(
                        arg, target_asset=target_asset, target_freq=target_freq
                    )
                    for arg in expr.args
                ),
                self._rewrite_kwargs(
                    expr.kwargs, target_asset=target_asset, target_freq=target_freq
                ),
            )
        return expr

    def _rewrite_value(self, value: Any, *, target_asset: str, target_freq: str) -> Any:
        """递归规划参数中的表达式值。"""
        if isinstance(value, Expr):
            return self._rewrite(
                value, target_asset=target_asset, target_freq=target_freq
            )
        return value

    def _rewrite_kwargs(
        self, kwargs: dict[str, Any], *, target_asset: str, target_freq: str
    ) -> dict[str, Any]:
        """递归规划算子的关键字参数。"""
        return {
            key: self._rewrite_value(
                value, target_asset=target_asset, target_freq=target_freq
            )
            for key, value in kwargs.items()
        }

    def _rewrite_intraday_source_arg(self, expr: Expr, *, target_asset: str) -> Expr:
        """在日频输出的高频聚合算子中保留高频输入。"""
        # 高频叶子保留自身频率，仅在资产类型不同时做资产规划。
        if isinstance(expr, (FeatureExpr, SourceExpr)):
            fk = parse_feature_key(expr.key)
            if fk.asset != target_asset:
                return self._rewrite_feature_node(
                    expr, expr.key, target_asset=target_asset, target_freq=fk.freq
                )
            return expr
        if isinstance(expr, RuntimeFeatureExpr):
            fk = parse_feature_key(expr.key)
            if fk.asset != target_asset:
                return self._rewrite_feature_node(
                    expr, expr.key, target_asset=target_asset, target_freq=fk.freq
                )
            return expr
        # 复合表达式递归保持其高频来源语义。
        if isinstance(expr, OpExpr):
            return OpExpr(
                expr.op,
                tuple(
                    self._rewrite_intraday_source_arg(arg, target_asset=target_asset)
                    for arg in expr.args
                ),
                {
                    key: self._rewrite_intraday_source_value(
                        value, target_asset=target_asset
                    )
                    for key, value in expr.kwargs.items()
                },
            )
        return expr

    def _rewrite_intraday_source_value(self, value: Any, *, target_asset: str) -> Any:
        """规划高频聚合算子的 source 参数。"""
        if isinstance(value, Expr):
            return self._rewrite_intraday_source_arg(value, target_asset=target_asset)
        return value

    def _rewrite_feature(
        self, expr: FeatureExpr, *, target_asset: str, target_freq: str
    ) -> Expr:
        """把单个 feature 对齐到目标资产和频率。"""
        node = self._runtime_or_feature(expr.key)
        return self._rewrite_feature_node(
            node, expr.key, target_asset=target_asset, target_freq=target_freq
        )

    def _runtime_or_feature(self, key: str) -> Expr:
        """同一计算会话内优先使用 runtime feature，否则使用已物化 feature。"""
        key = parse_feature_key(key).key
        if key in self.runtime_features:
            return RuntimeFeatureExpr(key)
        return FeatureExpr(key)

    def _rewrite_feature_node(
        self, node: Expr, key: str, *, target_asset: str, target_freq: str
    ) -> Expr:
        """把 feature-like leaf 对齐到目标资产和频率。"""
        # 同资产只处理频率，股票到转债使用显式映射，其余跨资产拒绝。
        fk = parse_feature_key(key)
        if fk.asset == target_asset:
            return self._rewrite_freq(
                node, feature_key=key, feature_freq=fk.freq, target_freq=target_freq
            )
        if target_asset == "cb" and fk.asset == "stk":
            return self._rewrite_stk_to_cb(node, key, target_freq=target_freq)
        if fk.asset == "idx" and target_asset != "idx":
            raise ValueError(
                f"{key} has asset axis idx and cannot align to {target_asset} without an index selector. "
                "Use broadcast_index_feature(index=..., feature=...)."
            )
        raise ValueError(f"Cannot align feature {key} to target asset {target_asset}")

    def _rewrite_stk_to_cb(self, node: Expr, key: str, *, target_freq: str) -> Expr:
        """用标准正股列映射把股票特征改写到转债空间。"""
        fk = parse_feature_key(key)
        source = self._rewrite_freq(
            node, feature_key=key, feature_freq=fk.freq, target_freq=target_freq
        )
        map_key = "cb.1d.underlying_stk_col"
        return OpExpr("lookup_by_col", (source, FeatureExpr(map_key)), {})

    def _rewrite_freq(
        self, expr: Expr, *, feature_key: str, feature_freq: str, target_freq: str
    ) -> Expr:
        """处理同资产不同频率之间的对齐和广播。"""
        # 同频直接返回，日频与分钟频分别进入对应旧版转换路径。
        if feature_freq == target_freq:
            return expr
        if feature_freq == "1d" and is_intraday_freq(target_freq):
            return self._rewrite_daily_to_intraday(
                expr, feature_key=feature_key, target_freq=target_freq
            )
        if is_intraday_freq(feature_freq) and is_intraday_freq(target_freq):
            return self._rewrite_intraday_to_finer(
                expr, source_freq=feature_freq, target_freq=target_freq
            )
        raise ValueError(f"Cannot mix frequency {feature_freq} into {target_freq}")

    def _rewrite_daily_to_intraday(
        self, expr: Expr, *, feature_key: str, target_freq: str
    ) -> Expr:
        """先延迟日频叶子，保留单 step 供 NumPy 在后续算子中自动广播。"""
        node = expr
        periods = self.delay_dict.get(parse_feature_key(feature_key).key, self.delay_lf)
        if periods:
            node = OpExpr("delay", (node,), {"periods": periods, "axis": 0})
        return node

    def _rewrite_intraday_to_finer(
        self, expr: Expr, *, source_freq: str, target_freq: str
    ) -> Expr:
        """将较粗分钟特征按当日时间戳前向填充到细频率。"""
        # 细到粗必须显式重采样，粗到细使用预计算前向索引。
        source_minutes = parse_intraday_minutes(source_freq)
        target_minutes = parse_intraday_minutes(target_freq)
        if source_minutes < target_minutes:
            raise ValueError(
                f"Cannot implicitly resample fine frequency {source_freq} into coarse frequency {target_freq}; "
                "the legacy pipeline does not support this conversion"
            )
        step_index = get_ffill_step_index(source_freq, target_freq)
        return OpExpr("ffill_to_finer_steps", (expr, ConstExpr(step_index)), {})

    def _rewrite_broadcast_index(
        self, expr: BroadcastIndexFeatureExpr, *, target_asset: str, target_freq: str
    ) -> Expr:
        """把指数特征显式选择后广播到目标资产空间。"""
        return self._rewrite_idx_to_asset(
            expr, target_asset=target_asset, target_freq=target_freq
        )

    def _rewrite_idx_to_asset(
        self, expr: BroadcastIndexFeatureExpr, *, target_asset: str, target_freq: str
    ) -> Expr:
        """选取指数轴位置并广播到目标资产空间。"""
        # helper 参数先解析为唯一指数特征键及指数轴位置。
        feature_key = _resolve_index_feature_key(expr.feature, self.aliases)
        feature_freq = parse_feature_key(feature_key).freq
        idx_space = self.store.resolve_space(feature_key)
        pos = _resolve_index_pos(idx_space, expr.index)
        target_space = self.store.resolve_space(
            f"{target_asset}.{target_freq}.__space__"
        )
        # 旧版路径显式选择指数并构造目标资产数量的广播表达式。
        selected = OpExpr(
            "select_by_pos",
            (FeatureExpr(feature_key), ConstExpr(pos)),
            {"axis": 1, "keepdims": False},
        )
        broadcasted = OpExpr(
            "broadcast_ts", (selected,), {"n_assets": target_space.n_assets}
        )
        if feature_freq == target_freq:
            return broadcasted
        if feature_freq == "1d" and is_intraday_freq(target_freq):
            return self._rewrite_daily_to_intraday(
                broadcasted, feature_key=feature_key, target_freq=target_freq
            )
        raise ValueError(
            f"Cannot broadcast idx frequency {feature_freq} into {target_freq}"
        )

    def _apply_input_mask(self, expr: Expr, mask_expr: Expr) -> Expr:
        """把 input_mask 下推到表达式中的输入特征。"""
        # 数据叶子就地包裹掩码算子，复合算子递归下推。
        if isinstance(expr, (FeatureExpr, SourceExpr, RuntimeFeatureExpr)):
            return OpExpr("apply_mask", (expr, mask_expr), {})
        if isinstance(expr, OpExpr):
            return OpExpr(
                expr.op,
                tuple(self._apply_input_mask(arg, mask_expr) for arg in expr.args),
                {
                    key: self._apply_input_mask_value(value, mask_expr)
                    for key, value in expr.kwargs.items()
                },
            )
        return expr

    def _apply_input_mask_value(self, value: Any, mask_expr: Expr) -> Any:
        """递归向参数值中的输入叶子应用 input_mask。"""
        if isinstance(value, Expr):
            return self._apply_input_mask(value, mask_expr)
        return value

    def _inject_sample_mask(self, expr: Expr, mask_expr: Expr) -> Expr:
        """把 sample_mask 注入 sample-aware 算子。"""
        # 先递归处理子表达式，再检查当前算子的样本参数位置。
        if isinstance(expr, OpExpr):
            args = tuple(self._inject_sample_mask(arg, mask_expr) for arg in expr.args)
            kwargs = {
                key: self._inject_sample_mask_value(value, mask_expr)
                for key, value in expr.kwargs.items()
            }
            required_before_mask = SAMPLE_AWARE_OPS.get(expr.op)
            # 参数缺失时追加，显式 None 占位时则就地替换。
            if required_before_mask is not None and len(args) == required_before_mask:
                self.sample_mask_injections += 1
                return OpExpr(expr.op, (*args, mask_expr), kwargs)
            if (
                required_before_mask is not None
                and len(args) > required_before_mask
                and isinstance(args[required_before_mask], ConstExpr)
                and args[required_before_mask].value is None
            ):
                self.sample_mask_injections += 1
                replaced = (
                    *args[:required_before_mask],
                    mask_expr,
                    *args[required_before_mask + 1 :],
                )
                return OpExpr(expr.op, replaced, kwargs)
            return OpExpr(expr.op, args, kwargs)
        return expr

    def _inject_sample_mask_value(self, value: Any, mask_expr: Expr) -> Any:
        """递归向参数值中的 sample-aware 算子注入 sample_mask。"""
        if isinstance(value, Expr):
            return self._inject_sample_mask(value, mask_expr)
        return value


class Executor:
    """递归求值规划后表达式树的旧版执行器。"""

    def __init__(
        self,
        store: FeatureStore,
        *,
        operators: dict[str, OperatorSpec] | None = None,
        runtime_features: dict[str, FeatureArray | CalculationResult] | None = None,
        data_router: Any | None = None,
    ):
        """初始化表达式执行器。"""
        # 保存数据来源与算子表，并初始化单次执行叶子缓存。
        self.store = store
        self.operators = operators or default_operator_registry()
        self.runtime_features = runtime_features if runtime_features is not None else {}
        self.data_router = data_router
        self._leaf_cache: dict[str, FeatureArray] = {}
        self._scope: ExecutionScope | None = None

    def eval(
        self,
        expr: Expr,
        *,
        output_key: str,
        feature_def: FeatureDef | None = None,
        scope: ExecutionScope | None = None,
    ) -> EvalResult:
        """执行表达式并校验输出空间。"""
        # 每次顶层执行重置叶子缓存并绑定本次日期范围。
        target_fk = parse_feature_key(output_key)
        self._leaf_cache = {}
        self._scope = scope
        result = self._eval(expr)
        # 数组结果规范为三维，并按旧版规则补齐 singleton step。
        if isinstance(result.values, np.ndarray):
            target_space = self.store.resolve_space(
                feature_def or output_key, scope=scope
            )
            values = result.values
            if values.ndim == 2:
                values = values[:, :, None]
            if (
                values.shape != target_space.shape
                and values.ndim == 3
                and values.shape[:2] == target_space.shape[:2]
                and values.shape[2] == 1
                and target_space.steps > 1
            ):
                values = np.broadcast_to(values, target_space.shape).copy()
            if values.shape != target_space.shape:
                raise ValueError(
                    f"Formula result shape {values.shape} does not match target space "
                    f"{target_space.key} {target_space.shape}"
                )
            return EvalResult(
                values=values,
                asset=target_fk.asset,
                freq=target_fk.freq,
                space=target_space,
                key=output_key,
                missing_value=result.missing_value,
            )
        return result

    def _eval(self, expr: Expr) -> EvalResult:
        """递归求值单个表达式节点。"""
        # 常量与三类数据叶子分别转换为统一求值结果。
        if isinstance(expr, ConstExpr):
            return EvalResult(expr.value)
        if isinstance(expr, FeatureExpr):
            feature = self._load_feature_like(expr.key)
            return EvalResult(
                feature.values,
                asset=feature.asset,
                freq=feature.freq,
                space=feature.space,
                key=feature.key,
                missing_value=feature.missing_value,
            )
        if isinstance(expr, SourceExpr):
            feature = self._load_source(expr.spec)
            return EvalResult(
                feature.values,
                asset=feature.asset,
                freq=feature.freq,
                space=feature.space,
                key=feature.key,
                missing_value=feature.missing_value,
            )
        if isinstance(expr, RuntimeFeatureExpr):
            feature = self.runtime_features[expr.key]
            return EvalResult(
                feature.values,
                asset=feature.asset,
                freq=feature.freq,
                space=feature.space,
                key=feature.key,
                missing_value=feature.missing_value,
            )
        # 算子先递归求值全部表达式参数，再调用注册函数。
        if isinstance(expr, OpExpr):
            spec = self.operators.get(expr.op)
            if spec is None:
                raise KeyError(f"Operator {expr.op!r} is not registered")
            args = [self._eval(arg).values for arg in expr.args]
            kwargs = {
                key: self._eval(value).values if isinstance(value, Expr) else value
                for key, value in expr.kwargs.items()
            }
            return EvalResult(spec.func(*args, **kwargs))
        if isinstance(expr, BroadcastIndexFeatureExpr):
            raise RuntimeError(
                "BroadcastIndexFeatureExpr must be planned before execution"
            )
        raise TypeError(f"Unsupported Expr type {type(expr).__name__}")

    def _load_feature_like(self, key: str) -> FeatureArray:
        """按 runtime、Store、Router 顺序读取叶子，并在单次 eval 内缓存。"""
        # 运行时结果优先于物化特征，未物化输入最后交给 Router。
        key = parse_feature_key(key).key
        if key in self._leaf_cache:
            return self._leaf_cache[key]
        if key in self.runtime_features:
            feature = self.runtime_features[key]
        elif self.store.has_feature(key):
            feature = self.store.load_feature(key, scope=self._scope)
        elif self.data_router is not None:
            feature = self.data_router.read(key, self.store, scope=self._scope)
        else:
            raise KeyError(f"{key} not found in features and no DataRouter is attached")
        self._leaf_cache[key] = feature
        return feature

    def _load_source(self, spec: SourceSpec) -> FeatureArray:
        """按 Store、显式 SourceSpec 顺序读取参数化外部输入。"""
        # 显式数据源使用独立缓存键，避免与普通叶子混淆。
        key = parse_feature_key(spec.key).key
        cache_key = f"source:{key}"
        if cache_key in self._leaf_cache:
            return self._leaf_cache[cache_key]
        if self.store.has_feature(key):
            feature = self.store.load_feature(key, scope=self._scope)
        elif self.data_router is not None:
            feature = self.data_router.read_spec(spec, self.store, scope=self._scope)
        else:
            raise KeyError(f"{key} not found in features and no DataRouter is attached")
        self._leaf_cache[cache_key] = feature
        return feature


class Calculator:
    """串联解析、规划与执行的旧版低层公式计算器。"""

    def __init__(
        self,
        snapshot: str | Path | FeatureStore,
        *,
        operators: dict[str, OperatorSpec] | None = None,
        data_router: Any | None = None,
    ):
        """初始化低层公式计算器。"""
        # 统一快照路径和存储对象，并建立会话级运行时特征缓存。
        self.store = (
            snapshot if isinstance(snapshot, FeatureStore) else FeatureStore(snapshot)
        )
        self.operators = operators or default_operator_registry()
        self.data_router = data_router
        self.runtime_features: dict[str, CalculationResult] = {}
        self.parser = FormulaParser()

    def calculate(
        self,
        formula: str | Expr,
        *,
        output: str,
        input_mask: str | Expr | None = None,
        sample_mask: str | Expr | None = None,
        output_mask: str | Expr | None = None,
        delay_lf: int = 1,
        delay_dict: dict[str, int] | None = None,
        feature_def: FeatureDef | None = None,
        scope: ExecutionScope | None = None,
    ) -> CalculationResult:
        """解析、规划并执行公式，返回不含研究定义的计算结果。"""
        # 字符串先解析为表达式，再按输出领域和掩码配置完成规划。
        expr = self.parser.parse(formula) if isinstance(formula, str) else formula

        planner = Planner(
            self.store,
            aliases={},
            runtime_features=self.runtime_features,
            delay_lf=delay_lf,
            delay_dict=delay_dict,
        )
        planned = planner.plan(
            expr,
            output_key=output,
            input_mask=input_mask,
            sample_mask=sample_mask,
            output_mask=output_mask,
        )
        # 未注入任何样本算子时保留诊断并提醒调用方。
        diagnostics: dict[str, Any] = {}
        if sample_mask is not None and planner.sample_mask_injections == 0:
            msg = "sample_mask was provided but no sample-aware operator was found; sample_mask was not applied."
            warnings.warn(msg, UserWarning, stacklevel=2)
            diagnostics["sample_mask_warning"] = msg

        # 执行规划表达式，并把结果缓存为后续公式可引用的运行时特征。
        executor = Executor(
            self.store,
            operators=self.operators,
            runtime_features=self.runtime_features,
            data_router=self.data_router,
        )
        result = executor.eval(
            planned, output_key=output, feature_def=feature_def, scope=scope
        )
        calculation = CalculationResult(
            key=output,
            values=result.values,
            space=result.space,
            missing_value=result.missing_value,
            diagnostics=diagnostics,
        )
        self.runtime_features[output] = calculation
        return calculation

    def debug_expr(
        self,
        expr: str | Expr,
        *,
        output: str | None = None,
        delay_lf: int = 1,
        delay_dict: dict[str, int] | None = None,
    ) -> str:
        """打印 parser 或 planner 后的表达式。"""
        # 未提供输出键时展示解析树，否则展示完整规划树。
        parsed = self.parser.parse(expr) if isinstance(expr, str) else expr
        if output is None:
            return format_expr(parsed)
        planner = Planner(
            self.store,
            aliases={},
            runtime_features=self.runtime_features,
            delay_lf=delay_lf,
            delay_dict=delay_dict,
        )
        planned = planner.plan(parsed, output_key=output)
        return format_expr(planned)


def _mask_expr(mask: str | Expr | bool, parser: FormulaParser | None = None) -> Expr:
    """把 mask 参数转换成 Expr。"""
    if isinstance(mask, Expr):
        return mask
    if isinstance(mask, bool):
        return ConstExpr(mask)
    if isinstance(mask, str):
        return (parser or FormulaParser()).parse(mask)
    raise TypeError(
        "mask must be a feature key string, expression string, Expr, bool, or None"
    )


def _collect_raw_dependencies(expr: Expr) -> tuple[str, ...]:
    """收集表达式树中的普通 FeatureExpr 依赖。"""
    # 深度优先遍历数据参数和表达式关键字参数。
    deps: list[str] = []

    def visit(node: Expr) -> None:
        """递归访问表达式节点并记录依赖。"""
        # 普通特征和指数 helper 记录完整键，复合算子继续向下遍历。
        if isinstance(node, FeatureExpr):
            deps.append(parse_feature_key(node.key).key)
        elif isinstance(node, AliasExpr):
            pass
        elif isinstance(node, OpExpr):
            for arg in node.args:
                visit(arg)
            for value in node.kwargs.values():
                if isinstance(value, Expr):
                    visit(value)
        elif isinstance(node, BroadcastIndexFeatureExpr):
            deps.append(parse_feature_key(node.feature).key)

    visit(expr)
    return tuple(deps)


def _collect_source_specs(expr: Expr) -> tuple[SourceSpec, ...]:
    """收集表达式树中的显式 SourceExpr 输入规格。"""
    specs: list[SourceSpec] = []

    def visit(node: Expr) -> None:
        """递归访问表达式节点并收集外部数据源规格。"""
        # 仅 SourceExpr 产生规格，复合算子继续遍历其表达式参数。
        if isinstance(node, SourceExpr):
            specs.append(node.spec)
        elif isinstance(node, OpExpr):
            for arg in node.args:
                visit(arg)
            for value in node.kwargs.values():
                if isinstance(value, Expr):
                    visit(value)

    visit(expr)
    # 按数据源键去重，同时拒绝同键但参数冲突的规格。
    seen: dict[str, SourceSpec] = {}
    unique: list[SourceSpec] = []
    for spec in specs:
        existing = seen.get(spec.key)
        if existing is None:
            unique.append(spec)
            seen[spec.key] = spec
        elif existing != spec:
            raise ValueError(
                f"Source key {spec.key!r} is used with conflicting SourceSpec values"
            )
    return tuple(unique)


_DATE_ROLLING_OPS = {"ts_mean", "ts_sum", "ts_std", "ts_min", "ts_max"}
_INTRADAY_DATE_ROLLING_OPS = {
    "intraday_flat_mean",
    "intraday_flat_std",
    "intraday_by_step_mean",
    "intraday_by_step_std",
}


def infer_date_overlap(expr: Expr) -> int:
    """推导分块执行表达式所需向前读取的日期数量。"""
    if isinstance(expr, OpExpr):
        # 父节点先继承全部子表达式中的最大历史窗口。
        child_overlap = 0
        for arg in expr.args:
            child_overlap = max(child_overlap, infer_date_overlap(arg))
        for value in expr.kwargs.values():
            if isinstance(value, Expr):
                child_overlap = max(child_overlap, infer_date_overlap(value))
        # 日期轴上的延迟和滚动算子会在子窗口之上增加历史需求。
        if expr.op == "delay" and int(expr.kwargs.get("axis", 0)) == 0:
            return child_overlap + max(0, _overlap_int(expr.kwargs.get("periods", 1)))
        if expr.op in _DATE_ROLLING_OPS and int(expr.kwargs.get("axis", 0)) == 0:
            return child_overlap + max(
                0, _overlap_int(expr.kwargs.get("window", 5)) - 1
            )
        if expr.op in _INTRADAY_DATE_ROLLING_OPS:
            return child_overlap + max(
                0, _overlap_int(expr.kwargs.get("window_days", 1)) - 1
            )
        return child_overlap
    return 0


def _overlap_int(value: Any) -> int:
    """将重叠窗口参数校验并转换为整数。"""
    if isinstance(value, ConstExpr):
        value = value.value
    if isinstance(value, Expr):
        return 0
    return int(value)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    """按原始顺序去重字符串序列。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _resolve_index_pos(space: FeatureSpace, index: str) -> int:
    """根据显式 InnerCode 映射解析指数在 Snapshot 轴中的位置。"""
    name = str(index).upper()
    if name not in INDEX_INNER_CODES:
        raise KeyError(f"Index {index!r} is not configured in INDEX_INNER_CODES")
    inner_code = INDEX_INNER_CODES[name]
    if inner_code is None:
        raise KeyError(f"Index {name!r} does not have a confirmed InnerCode yet")
    return space.code_to_pos(inner_code)


def _resolve_index_feature_key(
    feature: str, aliases: dict[str, Expr] | dict[str, str]
) -> str:
    """将指数广播参数解析为 idx 空间的完整特征键。"""
    # 参数可以是完整键，也可以是指向单一叶子的已注册别名。
    try:
        key = parse_feature_key(feature).key
    except ValueError:
        if feature not in aliases:
            raise KeyError(
                f"Index feature {feature!r} must be a full idx key or a registered alias"
            )
        value = aliases[feature]
        if isinstance(value, str):
            key = parse_feature_key(value).key
        elif isinstance(value, (FeatureExpr, RuntimeFeatureExpr)):
            key = parse_feature_key(value.key).key
        else:
            raise ValueError(
                f"Index feature alias {feature!r} must resolve to one full idx key"
            )
    # 最终解析结果必须明确属于指数资产轴。
    if parse_feature_key(key).asset != "idx":
        raise ValueError(f"Index feature {feature!r} must resolve to an idx.* key")
    return key


def normalize_registered_expr(expr: Expr, aliases: dict[str, str]) -> Expr:
    """在注册阶段冻结别名和 helper 参数，返回稳定表达式。"""
    # 各类叶子规范为持久化允许的完整键表示。
    if isinstance(expr, AliasExpr):
        if expr.name not in aliases:
            raise KeyError(f"Alias {expr.name!r} is not registered")
        return FeatureExpr(parse_feature_key(aliases[expr.name]).key)
    if isinstance(expr, FeatureExpr):
        return FeatureExpr(parse_feature_key(expr.key).key)
    if isinstance(expr, SourceExpr):
        parse_feature_key(expr.key)
        return expr
    if isinstance(expr, RuntimeFeatureExpr):
        raise ValueError("RuntimeFeatureExpr cannot be persisted in a FeatureDef")
    if isinstance(expr, BroadcastIndexFeatureExpr):
        key = _resolve_index_feature_key(expr.feature, aliases)
        return BroadcastIndexFeatureExpr(
            index=expr.index, feature=key, freq=parse_feature_key(key).freq
        )
    # 算子递归规范化所有表达式参数。
    if isinstance(expr, OpExpr):
        return OpExpr(
            expr.op,
            tuple(normalize_registered_expr(arg, aliases) for arg in expr.args),
            {
                key: normalize_registered_expr(value, aliases)
                if isinstance(value, Expr)
                else value
                for key, value in expr.kwargs.items()
            },
        )
    return expr


def _expr_to_dict(expr: Expr) -> dict[str, Any]:
    """把 Expr 转换为可序列化字典。"""
    # 每类表达式使用显式 type 标签形成唯一字典表示。
    if isinstance(expr, FeatureExpr):
        return {"type": "feature", "key": expr.key}
    if isinstance(expr, SourceExpr):
        return {"type": "source", "spec": expr.spec.to_dict()}
    if isinstance(expr, RuntimeFeatureExpr):
        return {"type": "runtime", "key": expr.key}
    if isinstance(expr, ConstExpr):
        return {"type": "const", "value": expr.value}
    if isinstance(expr, OpExpr):
        return {
            "type": "op",
            "op": expr.op,
            "args": [_expr_to_dict(arg) for arg in expr.args],
            "kwargs": {
                key: _expr_to_dict(value) if isinstance(value, Expr) else value
                for key, value in expr.kwargs.items()
            },
        }
    if isinstance(expr, AliasExpr):
        return {"type": "alias", "name": expr.name}
    if isinstance(expr, BroadcastIndexFeatureExpr):
        return {
            "type": "broadcast_index_feature",
            "index": expr.index,
            "feature": expr.feature,
            "freq": expr.freq,
        }
    return {"type": type(expr).__name__}


def _expr_from_dict(payload: dict[str, Any]) -> Expr:
    """从唯一字典表示还原 Expr 树。"""
    # type 标签决定节点类型，旧 raw 表示明确拒绝自动迁移。
    kind = payload.get("type")
    if kind == "raw":
        raise ValueError(
            "Expression type 'raw' is obsolete; migrate it to type 'feature'"
        )
    if kind == "feature":
        return FeatureExpr(payload["key"])
    if kind == "source":
        spec = payload["spec"]
        return SourceExpr(
            SourceSpec.from_key(
                spec["key"],
                source=spec.get("source"),
                table=spec.get("table"),
                field=spec.get("field"),
                params=spec.get("params", {}),
            )
        )
    if kind == "runtime":
        return RuntimeFeatureExpr(payload["key"])
    if kind == "const":
        return ConstExpr(payload.get("value"))
    if kind == "alias":
        return AliasExpr(payload["name"])
    if kind == "broadcast_index_feature":
        return BroadcastIndexFeatureExpr(
            index=payload["index"],
            feature=payload["feature"],
            freq=payload.get("freq", "1d"),
        )
    # 算子数据参数和表达式关键字参数递归还原。
    if kind == "op":
        return OpExpr(
            payload["op"],
            tuple(_expr_from_dict(arg) for arg in payload.get("args", [])),
            {
                key: _expr_from_dict(value)
                if isinstance(value, dict) and "type" in value
                else value
                for key, value in payload.get("kwargs", {}).items()
            },
        )
    raise ValueError(f"Unsupported expression payload type {kind!r}")


def format_expr(expr: Expr, indent: int = 0) -> str:
    """把 Expr 格式化为便于 debug 的多行字符串。"""
    # 叶子节点使用紧凑单行表示。
    pad = " " * indent
    if isinstance(expr, ConstExpr):
        return pad + repr(expr.value)
    if isinstance(expr, FeatureExpr):
        return pad + f'feature("{expr.key}")'
    if isinstance(expr, SourceExpr):
        return pad + f'source("{expr.key}", source={expr.spec.source!r})'
    if isinstance(expr, AliasExpr):
        return pad + f'alias("{expr.name}")'
    if isinstance(expr, RuntimeFeatureExpr):
        return pad + f'runtime("{expr.key}")'
    if isinstance(expr, BroadcastIndexFeatureExpr):
        return (
            pad
            + f'broadcast_index_feature(index="{expr.index}", feature="{expr.feature}", freq="{expr.freq}")'
        )
    # 复合算子按缩进递归展开数据参数和关键字参数。
    if isinstance(expr, OpExpr):
        if not expr.args and not expr.kwargs:
            return pad + f"{expr.op}()"
        lines = [pad + f"{expr.op}("]
        for arg in expr.args:
            lines.append(format_expr(arg, indent + 2) + ",")
        for key, value in expr.kwargs.items():
            if isinstance(value, Expr):
                lines.append(" " * (indent + 2) + f"{key}=")
                lines.append(format_expr(value, indent + 4) + ",")
            else:
                lines.append(" " * (indent + 2) + f"{key}={value!r},")
        lines.append(pad + ")")
        return "\n".join(lines)
    return pad + repr(expr)


def as_expr(value: str | Expr) -> Expr:
    """把字符串 feature key 或 Expr 统一转换为 Expr。"""
    if isinstance(value, Expr):
        return value
    return FeatureExpr(value)


def source(
    key: str,
    *,
    source: str | None = None,
    table: str | None = None,
    field: str | None = None,
    params: dict[str, Any] | None = None,
) -> SourceExpr:
    """构造携带显式读取参数的外部数据源表达式。"""
    # 统一委托 SourceSpec 完成完整键字段解析。
    return SourceExpr(
        SourceSpec.from_key(
            key,
            source=source,
            table=table,
            field=field,
            params=params,
        )
    )


def broadcast_index_feature(
    index: str, feature: str, freq: str = "1d"
) -> BroadcastIndexFeatureExpr:
    """构造显式指数广播表达式。"""
    return BroadcastIndexFeatureExpr(index=index, feature=feature, freq=freq)


_INDUSTRY_STAT_OPS = {
    "mean": "group_mean",
    "sum": "group_sum",
    "std": "group_std",
    "demean": "group_demean",
    "zscore": "group_zscore",
}


_MEMBER_STAT_OPS = {
    "mean": "member_mean",
    "sum": "member_sum",
    "std": "member_std",
    "demean": "member_demean",
    "zscore": "member_zscore",
}


def industry_stat(
    x: str | Expr,
    *,
    method: str = "zscore",
    system: str = "SW2021",
    level: int = 1,
    freq: str = "1d",
    sample_mask: str | Expr | None = None,
    weight: str | Expr | None = None,
) -> OpExpr:
    """构造行业分组统计表达式。"""
    # 业务方法名称先映射到底层通用分组算子。
    if method not in _INDUSTRY_STAT_OPS:
        raise ValueError(
            f"Unsupported industry stat {method!r}; expected one of {sorted(_INDUSTRY_STAT_OPS)}"
        )
    # 行业分类列作为普通输入传入，并按需附加样本掩码和权重。
    args: list[Expr] = [
        as_expr(x),
        FeatureExpr(f"stk.{freq}.industry_code.{system}.L{level}"),
        ConstExpr(None) if sample_mask is None else as_expr(sample_mask),
    ]
    if weight is not None:
        args.append(as_expr(weight))
    return OpExpr(_INDUSTRY_STAT_OPS[method], tuple(args), {})


def industry_mean(
    x: str | Expr,
    *,
    system: str = "SW2021",
    level: int = 1,
    freq: str = "1d",
    weight: str | None = None,
) -> OpExpr:
    """构造行业分组均值表达式。"""
    # 均值 helper 复用通用行业统计表达式构造器。
    return industry_stat(
        x, method="mean", system=system, level=level, freq=freq, weight=weight
    )


def industry_demean(
    x: str | Expr,
    *,
    system: str = "SW2021",
    level: int = 1,
    freq: str = "1d",
    weight: str | None = None,
) -> OpExpr:
    """构造行业分组去均值表达式。"""
    # 去均值 helper 复用通用行业统计表达式构造器。
    return industry_stat(
        x, method="demean", system=system, level=level, freq=freq, weight=weight
    )


def industry_zscore(
    x: str | Expr,
    *,
    system: str = "SW2021",
    level: int = 1,
    freq: str = "1d",
) -> OpExpr:
    """构造行业分组 zscore 表达式。"""
    return industry_stat(x, method="zscore", system=system, level=level, freq=freq)


def index_member_stat(
    x: str | Expr,
    *,
    index: str,
    method: str = "mean",
    freq: str = "1d",
    sample_mask: str | Expr | None = None,
    weight: str | Expr | None = None,
    broadcast: bool = False,
) -> OpExpr:
    """将指数成员统计展开为 member 算子和可选截面广播。"""
    # 业务统计名称映射到通用成员算子，成员列也是普通特征输入。
    if method not in _MEMBER_STAT_OPS:
        raise ValueError(
            f"Unsupported index member stat {method!r}; expected one of {sorted(_MEMBER_STAT_OPS)}"
        )
    member = FeatureExpr(f"stk.{freq}.is_member.{index}")
    args: list[Expr] = [
        as_expr(x),
        member,
        ConstExpr(None) if sample_mask is None else as_expr(sample_mask),
    ]
    if weight is not None:
        args.append(as_expr(weight))
    # reduce 默认保留 singleton 资产轴，仅显式请求时插入旧广播算子。
    stat = OpExpr(_MEMBER_STAT_OPS[method], tuple(args), {})
    return OpExpr("broadcast_cs", (stat,), {}) if broadcast else stat
