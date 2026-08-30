"""把公式批次编译为共享 Term 逻辑计划与已解析输出域。"""

from __future__ import annotations

import inspect
import operator
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from .domain import (
    ValueKind,
    get_ffill_step_index,
    get_freq_step_values,
    get_resample_group_index,
    get_step_values,
    is_intraday_freq,
    normalize_date_key,
    normalize_periods,
    normalize_runtime_axis,
    stable_hash,
)
from .formula import (
    DEFAULT_HELPERS,
    Expr,
    HelperExpr,
    LiteralExpr,
    OperatorExpr,
    SourceRefExpr,
)
from .model import (
    CompiledJob,
    CompileError,
    ComputeRequest,
    DataProvider,
    DataProviderError,
    DomainError,
    DomainSpec,
    InputSpec,
    LiteralTerm,
    LogicalPlan,
    OperatorTerm,
    ResolvedOutputDomain,
    SourceTerm,
    Term,
    TermDomain,
)
from .operators import OperatorSpec, VariadicInput, default_operator_registry
from .operators.domain_rules import numpy_domain


class Compiler:
    """把公式批次降低为 Term 逻辑计划并解析任务输出域的编译器。"""

    def __init__(
        self,
        provider: DataProvider,
        operators: Mapping[str, OperatorSpec] | None = None,
    ) -> None:
        """使用数据提供者和运算符表初始化编译器。"""
        self.provider = provider
        self.operators = dict(operators or default_operator_registry())

    def compile(self, request: ComputeRequest) -> CompiledJob:
        """把计算请求编译为共享 Term DAG 和已解析输出域。"""
        # 绑定公式符号并展开 helper，得到只含数据源和算子的规范表达式。
        bound = request.batch.bind(reserved_names=set(self.operators) | DEFAULT_HELPERS)
        outputs = {
            formula_id: self._expand_helpers(expr)
            for formula_id, expr in bound.outputs.items()
        }
        # 与正式 lowering 共用参数规范化规则，先得到任务轴所需回看范围。
        lookbacks: list[int] = []
        for formula_id, expr in outputs.items():
            try:
                lookbacks.append(_expression_lookback(expr, self.operators))
            except CompileError as exc:
                location = str(expr.span) if expr.span is not None else formula_id
                raise type(exc)(f"{location}: formula {formula_id!r}: {exc}") from exc
        pre_lookback = max(lookbacks, default=0)
        # 批量获取输入描述，并据此解析本次任务的输出坐标域。
        refs = tuple(dict.fromkeys(_source_refs(outputs.values())))
        input_specs = dict(self.provider.describe_many(refs))
        if set(input_specs) != set(refs):
            missing = set(refs) - set(input_specs)
            raise DataProviderError(f"describe_many omitted sources: {missing}")
        domain = self._resolve_domain(
            request.domain, input_specs.values(), pre_lookback
        )

        # 初始化单次编译状态，将各输出降低到共享的 Term DAG。
        self._terms: dict[str, Term] = {}
        self._by_semantic_key: dict[str, str] = {}
        self._order: list[str] = []
        self._input_specs = input_specs
        self._target_domain = _term_domain(domain)
        lowered_outputs: dict[str, str] = {}
        for formula_id, expr in outputs.items():
            try:
                lowered_outputs[formula_id] = self._lower(expr)
            except CompileError as exc:
                location = str(expr.span) if expr.span is not None else formula_id
                raise type(exc)(f"{location}: formula {formula_id!r}: {exc}") from exc
        for formula_id, term_id in lowered_outputs.items():
            self._validate_output_domain(formula_id, self._terms[term_id].domain)
        # 统计引用次数供执行期回收中间结果，并计算任务级回看长度。
        references = {term_id: 0 for term_id in self._terms}
        for term in self._terms.values():
            if isinstance(term, OperatorTerm):
                for dependency in term.input_term_ids:
                    references[dependency] += 1
        job_lookback = max(
            self._terms[term_id].lookback for term_id in lowered_outputs.values()
        )
        if job_lookback != pre_lookback:
            raise CompileError(
                "AST lookback pre-analysis disagrees with the lowered LogicalPlan"
            )
        # 计划身份仅由稳定语义和输出映射决定。
        semantic_id = stable_hash(
            tuple(
                (term_id, self._terms[term_id].semantic_key) for term_id in self._order
            ),
            tuple(lowered_outputs.items()),
        )
        return CompiledJob(
            LogicalPlan(
                MappingProxyType(dict(self._terms)),
                tuple(self._order),
                MappingProxyType(lowered_outputs),
                MappingProxyType(references),
                job_lookback,
                semantic_id,
            ),
            domain,
        )

    def _resolve_domain(
        self,
        spec: DomainSpec,
        input_specs: Sequence[InputSpec],
        lookback: int,
    ) -> ResolvedOutputDomain:
        """根据请求范围和输入描述解析不可变输出域。"""
        # 目标资产必须在请求范围内，全部输入必须共用同一日历。
        if spec.target_asset not in spec.asset_scope:
            raise DomainError("target_asset must be present in asset_scope")
        calendars = {item.calendar for item in input_specs}
        if len(calendars) > 1:
            raise DomainError(
                f"Sources use incompatible calendars: {sorted(calendars)}"
            )
        calendar = next(iter(calendars), "default")
        # 在提供者日历中裁剪闭区间日期，并按回看长度外扩成分解析窗口。
        all_dates = np.asarray(self.provider.calendar_dates(calendar))
        start, end = normalize_date_key(spec.start), normalize_date_key(spec.end)
        matched = np.flatnonzero((all_dates >= start) & (all_dates <= end))
        if matched.size == 0:
            raise DomainError(f"No output dates between {start} and {end}")
        dates = all_dates[matched]
        axis_dates = tuple(
            all_dates[max(0, matched[0] - lookback) : matched[-1] + 1].tolist()
        )
        missing_assets = {
            item.asset_type
            for item in input_specs
            if item.asset_type not in spec.asset_scope
        }
        if missing_assets:
            raise DomainError(
                f"asset_scope does not declare source assets: {sorted(missing_assets)}"
            )
        # 校验并固定每类资产的有序代码轴及其指纹。
        self._asset_axes: dict[str, tuple[tuple[Any, ...], str]] = {}
        for asset, selector in spec.asset_scope.items():
            # 先校验选择器，再向提供者核对成分与显式代码。
            requested = None
            if isinstance(selector, str):
                if selector != "all":
                    raise DomainError(
                        f"Unsupported asset selector {selector!r}; first version only supports 'all'"
                    )
            else:
                requested = tuple(selector)
                if not requested:
                    raise DomainError("Explicit asset subset must not be empty")
                if len(set(requested)) != len(requested):
                    raise DomainError("Explicit asset subset contains duplicates")
            master = tuple(
                np.asarray(
                    self.provider.asset_codes(asset, axis_dates, selector)
                ).tolist()
            )
            if requested is not None:
                requested = tuple(np.asarray(requested).tolist())
                unknown = [code for code in requested if code not in set(master)]
                if unknown:
                    raise DomainError(f"Unknown asset codes: {unknown}")
            codes = requested if requested is not None else master
            self._asset_axes[asset] = (codes, stable_hash(calendar, asset, codes))
        # 频率与 step 数共同确定第三维坐标；输出指纹复用目标资产轴指纹。
        codes = np.asarray(self._asset_axes[spec.target_asset][0])
        try:
            steps = get_step_values(spec.target_freq, spec.target_step_count)
        except (TypeError, ValueError) as exc:
            raise DomainError(str(exc)) from exc
        return ResolvedOutputDomain(
            dates,
            spec.target_asset,
            codes,
            spec.target_freq,
            steps,
            calendar,
            self._asset_axes[spec.target_asset][1],
        )

    def _expand_helpers(self, expr: Expr) -> Expr:
        """递归把 helper 表达式展开为数据源或运算符表达式。"""
        # 普通算子只递归处理其数据参数。
        if isinstance(expr, OperatorExpr):
            return OperatorExpr(
                expr.name,
                tuple(self._expand_helpers(arg) for arg in expr.args),
                tuple(
                    (
                        name,
                        self._expand_helpers(value)
                        if isinstance(value, Expr)
                        else value,
                    )
                    for name, value in expr.params
                ),
                span=expr.span,
            )
        if not isinstance(expr, HelperExpr):
            return expr
        # helper 参数先递归展开，再按名称转换为内部表达式。
        args = tuple(self._expand_helpers(arg) for arg in expr.args)
        params = dict(expr.params)
        if expr.name == "project_stk_to_cb":
            if len(args) != 1:
                raise CompileError("project_stk_to_cb() requires one stock expression")
            if params:
                raise CompileError("project_stk_to_cb() got unknown parameters")
            return OperatorExpr(
                "lookup_by_col",
                (
                    args[0],
                    SourceRefExpr.create("cb.1d.underlying_stk_col"),
                ),
                span=expr.span,
            )
        if expr.name == "source":
            values = [_literal_value(expr.name, arg) for arg in args]
            if len(values) != 1:
                raise CompileError("source() requires one logical key")
            return SourceRefExpr.create(str(values[0]), **params)
        if expr.name == "get_lf":
            values = [_literal_value(expr.name, arg) for arg in args]
            if len(values) != 2:
                raise CompileError("get_lf() requires asset and feature")
            return SourceRefExpr.create(f"{values[0]}.1d.{values[1]}", **params)
        if expr.name == "get_hf":
            values = [_literal_value(expr.name, arg) for arg in args]
            if len(values) != 3:
                raise CompileError("get_hf() requires asset, frequency and feature")
            target_freq = params.pop("resample", None)
            method = params.pop("method", None)
            raw = SourceRefExpr.create(f"{values[0]}.{values[1]}.{values[2]}", **params)
            if target_freq is None:
                if method is not None:
                    raise CompileError("get_hf() method requires resample")
                return replace(raw, span=expr.span)
            if method is None:
                raise CompileError("get_hf() resample requires an explicit method")
            return OperatorExpr(
                "resample",
                (raw,),
                (("method", str(method)), ("target_freq", str(target_freq))),
                span=expr.span,
            )
        if expr.name == "get_fund":
            values = [_literal_value(expr.name, arg) for arg in args]
            if len(values) != 2:
                raise CompileError("get_fund() requires asset and feature")
            return SourceRefExpr.create(f"{values[0]}.1d.{values[1]}", **params)
        if expr.name == "load_factor":
            values = [_literal_value(expr.name, arg) for arg in args]
            if len(values) != 1:
                raise CompileError("load_factor() requires one factor id")
            return SourceRefExpr.create(f"factor:{values[0]}")
        if expr.name in {"select_asset", "select_index_feature"}:
            # 业务 helper 只解析代码，底层统一使用位置选择算子。
            if len(args) != 2:
                raise CompileError(f"{expr.name}() requires values and one code")
            code = _literal_value(expr.name, args[1])
            if params:
                raise CompileError(f"{expr.name}() got unknown parameters")
            internal_params = {"code": code}
            if expr.name == "select_index_feature":
                internal_params["expected_asset_type"] = "idx"
            return OperatorExpr(
                "__select_asset",
                (args[0],),
                tuple(sorted(internal_params.items())),
                span=expr.span,
            )
        if expr.name == "index_member_stat":
            if len(args) != 2:
                raise CompileError("index_member_stat() requires values and member")
            method = str(params.pop("method", "mean"))
            if params:
                raise CompileError("index_member_stat() got unknown parameters")
            operators = {
                "mean": "member_mean",
                "sum": "member_sum",
                "std": "member_std",
            }
            try:
                operator_name = operators[method]
            except KeyError as exc:
                raise CompileError(
                    f"index_member_stat() does not support method {method!r}"
                ) from exc
            return OperatorExpr(
                operator_name,
                args,
                span=expr.span,
            )
        raise CompileError(f"Unknown helper {expr.name!r}")

    def _lower(self, expr: Expr) -> str:
        """把规范表达式递归降低为 Term 并返回其标识。"""
        if isinstance(expr, LiteralExpr):
            return self._lower_literal(expr.value)
        if isinstance(expr, SourceRefExpr):
            return self._lower_source(expr)
        if isinstance(expr, OperatorExpr):
            return self._lower_operator(expr)
        raise CompileError(f"Canonical AST contains {type(expr).__name__}")

    def _lower_literal(self, value: Any) -> str:
        """把字面量规范化并降低为可复用的 LiteralTerm。"""
        value = _normalize_value(value)
        kind = ValueKind.MASK if isinstance(value, bool) else ValueKind.NUMERIC
        semantic = stable_hash("literal", value, kind.value)
        return self._intern(
            semantic,
            lambda term_id: LiteralTerm(term_id, kind, None, 0, semantic, value),
        )

    def _lower_source(self, ref: SourceRefExpr) -> str:
        """把逻辑数据源引用降低为保留原生领域的 SourceTerm。"""
        # 输入领域沿用提供者描述，仅校验其第三维是否合法。
        spec = self._input_specs[ref]
        codes, fingerprint = self._asset_axes[spec.asset_type]
        try:
            get_step_values(spec.frequency, spec.step_count)
        except (TypeError, ValueError) as exc:
            raise DomainError(
                f"Source {ref.logical_key!r} has an invalid Domain: {exc}"
            ) from exc
        domain = TermDomain(
            spec.asset_type,
            codes,
            spec.frequency,
            spec.step_count,
            spec.calendar,
            fingerprint,
        )
        semantic = stable_hash("source", _source_identity(ref), spec, domain)
        return self._intern(
            semantic,
            lambda term_id: SourceTerm(
                term_id, spec.value_kind, domain, 0, semantic, ref, spec
            ),
        )

    def _lower_operator(self, expr: OperatorExpr) -> str:
        """校验运算符语义并降低为可复用的 OperatorTerm。"""
        # 需要输入 Domain 派生 Runtime 参数的算子走专属 lowering。
        if expr.name == "resample":
            return self._lower_resample(expr)
        if expr.name == "align_frequency":
            return self._lower_align_frequency(expr)
        if expr.name == "__select_asset":
            return self._lower_select_asset(expr)
        try:
            spec = self.operators[expr.name]
        except KeyError as exc:
            raise CompileError(f"Unknown operator {expr.name!r}") from exc
        # 规范调用参数，降低输入并依照算子契约推导输出领域。
        input_exprs, input_names, params = _canonical_call(expr, spec)
        input_ids = tuple(self._lower(arg) for arg in input_exprs)
        inputs = tuple(self._terms[term_id] for term_id in input_ids)
        _validate_operator(spec, inputs, input_names, params)
        rule = spec.domain_rule or numpy_domain
        domain = rule(tuple(term.domain for term in inputs), params)
        params = dict(params)
        if spec.name == "get_step" and "step" in params:
            assert inputs[0].domain is not None
            params["step"] %= inputs[0].domain.step_count
        elif spec.name == "select_by_pos" and "pos" in params:
            assert inputs[0].domain is not None
            axis = params.get("axis", 1)
            length = (
                inputs[0].domain.asset_count
                if axis == 1
                else inputs[0].domain.step_count
            )
            params["pos"] %= length
        output_kind = _output_kind(spec, inputs)
        # 当前算子的窗口累加到最深输入依赖的回看长度上。
        local_lookback = _operator_lookback(spec, params)
        lookback = local_lookback + max((term.lookback for term in inputs), default=0)
        semantic = stable_hash(
            "operator",
            spec.name,
            tuple(zip(input_names, input_ids, strict=True)),
            params,
            output_kind.value,
            domain,
        )
        return self._intern(
            semantic,
            lambda term_id: OperatorTerm(
                term_id,
                output_kind,
                domain,
                lookback,
                semantic,
                spec.name,
                input_ids,
                input_names,
                MappingProxyType(dict(params)),
            ),
        )

    def _lower_resample(self, expr: OperatorExpr) -> str:
        """降低需要输入频率参与校验和执行的公开 resample 算子。"""
        # 复用通用调用规范化，先完成输入降低与契约校验。
        try:
            spec = self.operators[expr.name]
        except KeyError as exc:
            raise CompileError(f"Unknown operator {expr.name!r}") from exc
        input_exprs, input_names, params = _canonical_call(expr, spec)
        input_ids = tuple(self._lower(arg) for arg in input_exprs)
        inputs = tuple(self._terms[term_id] for term_id in input_ids)
        _validate_operator(spec, inputs, input_names, params)
        if len(inputs) != 1 or inputs[0].domain is None:
            raise DomainError("Cannot resample a scalar")
        if "source_freq" in params:
            raise CompileError("resample does not accept source_freq")
        input_term = inputs[0]
        input_domain = input_term.domain
        assert input_domain is not None
        source_freq = input_domain.frequency
        target_freq = str(params["target_freq"])
        method = params.get("method")
        if method is None:
            raise CompileError("resample requires an explicit method")
        method = str(method)
        if method not in {"mean", "sum", "std", "last"}:
            raise CompileError(f"Unsupported resample method {method!r}")
        # 校验源与目标频率组合以及源 step 轴的完整性。
        if source_freq == target_freq:
            raise DomainError(
                "resample requires different source and target frequencies"
            )
        if source_freq == "1d":
            raise DomainError("resample cannot convert a daily input")
        if input_domain.step_count != len(get_freq_step_values(source_freq)):
            raise DomainError(
                "resample requires the complete standard source step axis"
            )
        # 按目标频率生成 step 分组边界作为运行期参数。
        if target_freq == "1d":
            groups = np.zeros(input_domain.step_count, dtype=np.intp)
            step_count = 1
        else:
            try:
                groups, step_count = get_resample_group_index(source_freq, target_freq)
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
        starts = np.r_[0, np.flatnonzero(np.diff(groups)) + 1]
        stops = np.r_[starts[1:], len(groups)]
        boundaries = tuple(
            (int(start), int(stop)) for start, stop in zip(starts, stops, strict=True)
        )
        runtime_params = {
            "method": {"mean": 0, "sum": 1, "std": 2, "last": 3}[method],
            "boundaries": boundaries,
        }
        _validate_operator(spec, inputs, input_names, runtime_params)
        return self._domain_operator(
            spec.name,
            input_ids[0],
            runtime_params,
            replace(input_domain, frequency=target_freq, step_count=step_count),
        )

    def _lower_align_frequency(self, expr: OperatorExpr) -> str:
        """把用户显式声明的粗到细 ffill 降低为领域运算符。"""
        try:
            spec = self.operators[expr.name]
        except KeyError as exc:
            raise CompileError(f"Unknown operator {expr.name!r}") from exc
        input_exprs, input_names, params = _canonical_call(expr, spec)
        input_ids = tuple(self._lower(arg) for arg in input_exprs)
        inputs = tuple(self._terms[term_id] for term_id in input_ids)
        _validate_operator(spec, inputs, input_names, params)
        if len(inputs) != 1:
            raise CompileError("align_frequency requires one data expression")
        input_id, input_term = input_ids[0], inputs[0]
        if input_term.domain is None:
            raise DomainError("Cannot align a scalar frequency")
        params = dict(params)
        if "target_freq" not in params:
            raise CompileError(
                "align_frequency() requires an expression and target frequency"
            )
        if "method" not in params:
            raise CompileError("align_frequency() requires an explicit method")
        target_freq = str(params.pop("target_freq"))
        method = str(params.pop("method"))
        if params:
            raise CompileError(f"Unknown align_frequency parameters: {sorted(params)}")
        source_freq = input_term.domain.frequency
        if method != "ffill":
            raise CompileError("align_frequency first version only supports 'ffill'")
        if not is_intraday_freq(source_freq) or not is_intraday_freq(target_freq):
            raise DomainError(
                "align_frequency only supports coarse intraday to fine intraday"
            )
        if input_term.domain.step_count != len(get_freq_step_values(source_freq)):
            raise DomainError(
                "align_frequency requires the complete standard source step axis"
            )
        # 标准频率表给出细频 step 对应的粗频位置。
        try:
            step_index = get_ffill_step_index(source_freq, target_freq)
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        output_domain = replace(
            input_term.domain,
            frequency=target_freq,
            step_count=len(step_index),
        )
        return self._domain_operator(
            spec.name,
            input_id,
            {"step_index": tuple(step_index.tolist())},
            output_domain,
        )

    def _lower_select_asset(self, expr: OperatorExpr) -> str:
        """按稳定代码解析位置并降低为通用 keepdims 选择算子。"""
        # 选择操作要求输入具有可定位的命名资产轴。
        if len(expr.args) != 1:
            raise CompileError("asset selection requires one data expression")
        input_id = self._lower(expr.args[0])
        input_term = self._terms[input_id]
        if input_term.domain is None or input_term.domain.codes is None:
            raise DomainError("asset selection requires a named asset axis")
        params = dict(expr.params)
        code = params.pop("code")
        expected_asset_type = params.pop("expected_asset_type", None)
        if params:
            raise CompileError(f"Unknown asset selection parameters: {sorted(params)}")
        if (
            expected_asset_type is not None
            and input_term.domain.asset_type != expected_asset_type
        ):
            raise DomainError(
                f"Selection requires asset type {expected_asset_type!r}, got "
                f"{input_term.domain.asset_type!r}"
            )
        # 编译期将稳定代码解析为位置，运行期仅执行数组切片。
        try:
            position = input_term.domain.codes.index(code)
        except ValueError as exc:
            raise DomainError(
                f"Asset code {code!r} is not present on the input axis"
            ) from exc
        output_domain = replace(input_term.domain, codes=(code,))
        return self._domain_operator(
            "select_by_pos",
            input_id,
            {"pos": position, "axis": 1, "keepdims": True},
            output_domain,
        )

    def _validate_output_domain(
        self, formula_id: str, domain: TermDomain | None
    ) -> None:
        """校验输出坐标，仅放行已确认的 singleton 广播。"""
        # 标量与跨日历输出无法参与目标数组计算。
        if domain is None:
            raise DomainError(f"Formula {formula_id!r} output cannot be scalar")
        target = self._target_domain
        if domain.calendar != target.calendar:
            raise DomainError(
                f"Formula {formula_id!r} output calendar does not match target"
            )
        # 单资产轴可广播；多资产轴则必须与目标轴完全同一。
        if domain.asset_count != 1 and (
            domain.asset_type,
            domain.codes,
            domain.axis_fingerprint,
        ) != (
            target.asset_type,
            target.codes,
            target.axis_fingerprint,
        ):
            raise DomainError(
                f"Formula {formula_id!r} output asset axis does not match target"
            )
        # 同频率直接兼容，日频单 step 允许广播到分钟 step 轴。
        frequency_compatible = domain.frequency == target.frequency or (
            domain.frequency == "1d"
            and domain.step_count == 1
            and is_intraday_freq(target.frequency)
        )
        if not frequency_compatible:
            raise DomainError(
                f"Formula {formula_id!r} output frequency {domain.frequency!r} "
                f"does not match target {target.frequency!r}; align it explicitly"
            )
        if domain.step_count not in {1, target.step_count}:
            raise DomainError(
                f"Formula {formula_id!r} output step count {domain.step_count} "
                f"cannot broadcast to target step count {target.step_count}"
            )

    def _domain_operator(
        self,
        name: str,
        input_id: str,
        params: Mapping[str, Any],
        domain: TermDomain,
    ) -> str:
        """创建或复用一个具有显式输出领域的内部运算符 Term。"""
        # 参数先规范化，确保同语义内部转换能够命中公共子表达式。
        input_term = self._terms[input_id]
        normalized = {
            key: _normalize_value(value) for key, value in sorted(params.items())
        }
        semantic = stable_hash(
            "domain_operator",
            name,
            input_id,
            normalized,
            input_term.value_kind.value,
            domain,
        )
        return self._intern(
            semantic,
            lambda term_id: OperatorTerm(
                term_id,
                input_term.value_kind,
                domain,
                input_term.lookback,
                semantic,
                name,
                (input_id,),
                (None,),
                MappingProxyType(normalized),
            ),
        )

    def _intern(self, semantic: str, factory: Any) -> str:
        """按语义键复用 Term 或创建新的稳定 Term 标识。"""
        existing = self._by_semantic_key.get(semantic)
        if existing is not None:
            return existing
        term_id = f"term_{semantic[:16]}"
        self._terms[term_id] = factory(term_id)
        self._by_semantic_key[semantic] = term_id
        self._order.append(term_id)
        return term_id


def _source_refs(expressions: Sequence[Expr]) -> Iterator[SourceRefExpr]:
    """递归遍历一组表达式中的全部数据源引用。"""
    for expr in expressions:
        if isinstance(expr, SourceRefExpr):
            yield expr
        elif isinstance(expr, OperatorExpr):
            yield from _source_refs(expr.args)
            yield from _source_refs(
                tuple(value for _, value in expr.params if isinstance(value, Expr))
            )


def _expression_lookback(expr: Expr, operators: Mapping[str, OperatorSpec]) -> int:
    """在降低 DAG 前按同一算子契约计算表达式的累计日期回看。"""
    # 字面量和 Source 自身不引入额外历史；领域转换只透传依赖 horizon。
    if isinstance(expr, (LiteralExpr, SourceRefExpr)):
        return 0
    if not isinstance(expr, OperatorExpr):
        raise CompileError(f"Canonical AST contains {type(expr).__name__}")
    if expr.name == "__select_asset":
        return max(
            (_expression_lookback(arg, operators) for arg in expr.args), default=0
        )
    try:
        spec = operators[expr.name]
    except KeyError as exc:
        raise CompileError(f"Unknown operator {expr.name!r}") from exc
    # 参数规范化和局部 lookback 与正式 lowering 共用，避免维护第二套窗口语义。
    inputs, _, params = _canonical_call(expr, spec)
    return _operator_lookback(spec, params) + max(
        (_expression_lookback(arg, operators) for arg in inputs), default=0
    )


def _source_identity(ref: SourceRefExpr) -> tuple[Any, ...]:
    """返回数据源引用参与逻辑计划身份的规范结构。"""
    return ref.logical_key, tuple(
        (key, _normalize_value(value)) for key, value in ref.semantic_params
    )


def _term_domain(domain: ResolvedOutputDomain) -> TermDomain:
    """把已解析输出域转换为 Term 使用的不可变领域描述。"""
    return TermDomain(
        domain.asset_type,
        tuple(domain.codes.tolist()),
        domain.frequency,
        len(domain.steps),
        domain.calendar,
        domain.axis_fingerprint,
    )


def _literal_value(helper: str, expr: Expr) -> Any:
    """提取 helper 的字面量参数并拒绝动态表达式。"""
    if not isinstance(expr, LiteralExpr):
        raise CompileError(f"Helper {helper!r} arguments must be literals")
    return expr.value


def _canonical_call(
    expr: OperatorExpr, spec: OperatorSpec
) -> tuple[tuple[Expr, ...], tuple[str | None, ...], dict[str, Any]]:
    """规范化运算符的数据输入和字面量配置参数。"""
    # 按算子函数签名绑定调用参数，立即拒绝不匹配的调用。
    signature = inspect.signature(spec.func)
    try:
        bound = signature.bind(*expr.args, **dict(expr.params))
    except TypeError as exc:
        raise CompileError(
            f"Invalid arguments for operator {spec.name!r}: {exc}"
        ) from exc

    # 变长输入算子把全部实参直接作为无名数据输入。
    if isinstance(spec.input_kinds, VariadicInput):
        inputs = tuple(next(iter(bound.arguments.values()), ()))
        return inputs, (None,) * len(inputs), {}

    # 区分数据输入与字面量配置，并按算子契约逐个校验。
    required = tuple(signature.parameters)[: len(spec.input_kinds)]
    tensor_kinds = {
        **dict(zip(required, spec.input_kinds)),
        **dict(spec.optional_inputs),
    }
    inputs: dict[str, Expr] = {}
    params: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        if name not in tensor_kinds:
            try:
                params[name] = (
                    _configuration_literal(value) if isinstance(value, Expr) else value
                )
            except TypeError as exc:
                raise CompileError(
                    f"Operator {spec.name!r} configuration must be literal"
                ) from exc
        elif name in required:
            if not isinstance(value, Expr):
                raise CompileError(
                    f"Operator {spec.name!r} input {name!r} must be a Term"
                )
            inputs[name] = value
        elif not (
            value is None or isinstance(value, LiteralExpr) and value.value is None
        ):
            if not isinstance(value, Expr) or isinstance(value, LiteralExpr):
                raise CompileError(
                    f"Operator {spec.name!r} optional input {name!r} must be a Term or None"
                )
            inputs[name] = value

    present_required = tuple(name for name in required if name in inputs)
    present_optional = tuple(name for name, _ in spec.optional_inputs if name in inputs)
    names = present_required + present_optional
    return (
        tuple(inputs[name] for name in names),
        (None,) * len(present_required) + present_optional,
        _canonical_parameters(spec.name, params),
    )


def _canonical_parameters(name: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """在 Compiler 边界把 Runtime 配置规范为可直接计算的值。"""
    # 先做稳定可哈希的通用规范化，再逐项应用领域与取值约束。
    result = {key: _normalize_value(value) for key, value in sorted(params.items())}
    try:
        if "periods" in result:
            result["periods"] = normalize_periods(result["periods"])
        if "axis" in result:
            result["axis"] = normalize_runtime_axis(result["axis"])
    except ValueError as exc:
        raise CompileError(str(exc)) from exc
    for key in ("window", "window_days", "min_periods"):
        if key in result and result[key] is not None:
            result[key] = _positive_integer(result[key], key)
    if "limit" in result and result["limit"] is not None:
        result["limit"] = _nonnegative_integer(result["limit"], "limit")
    for key in ("step", "pos", "start", "end", "n_assets", "n_steps"):
        if key in result and result[key] is not None:
            result[key] = _integer_parameter(result[key], key)
    if (
        result.get("min_periods") is not None
        and result.get("window") is not None
        and result["min_periods"] > result["window"]
    ):
        raise CompileError("min_periods must not exceed window")
    # winsorize 的截断边界必须落在有序概率区间内。
    if name == "winsorize":
        lower = float(result.get("lower", 0.01))
        upper = float(result.get("upper", 0.99))
        if not 0.0 <= lower <= upper <= 1.0:
            raise CompileError("winsorize requires 0 <= lower <= upper <= 1")
        result.update(lower=lower, upper=upper)
    return result


def _integer_parameter(value: Any, name: str) -> int:
    """把配置值严格转换为整数，并拒绝布尔值等非整数类型。"""
    if isinstance(value, bool):
        raise CompileError(f"{name} must be an integer")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise CompileError(f"{name} must be an integer") from exc


def _positive_integer(value: Any, name: str) -> int:
    """校验并返回必须是正整数的配置参数。"""
    normalized = _integer_parameter(value, name)
    if normalized <= 0:
        raise CompileError(f"{name} must be positive")
    return normalized


def _nonnegative_integer(value: Any, name: str) -> int:
    """校验并返回必须是非负整数的配置参数。"""
    normalized = _integer_parameter(value, name)
    if normalized < 0:
        raise CompileError(f"{name} must be non-negative")
    return normalized


def _validate_operator(
    spec: OperatorSpec,
    inputs: Sequence[Term],
    input_names: Sequence[str | None],
    params: Mapping[str, Any],
) -> None:
    """校验运算符的输入数量、值类型和调用参数。"""
    # 根据固定或可变输入契约生成各位置期望的值类型。
    if isinstance(spec.input_kinds, VariadicInput):
        if len(inputs) < spec.input_kinds.min_count:
            raise CompileError(f"Operator {spec.name!r} has too few inputs")
        expected = (spec.input_kinds.kind,) * len(inputs)
    else:
        required_count = len(spec.input_kinds)
        if len(inputs) < required_count:
            raise CompileError(
                f"Operator {spec.name!r} requires {required_count} inputs, got {len(inputs)}"
            )
        optional = dict(spec.optional_inputs)
        expected = spec.input_kinds + tuple(
            optional[name] for name in input_names[required_count:]
        )
    for index, (term, kind) in enumerate(zip(inputs, expected)):
        if term.value_kind is not kind:
            raise CompileError(
                f"Operator {spec.name!r} input {index} requires {kind.value}, "
                f"got {term.value_kind.value}"
            )


def _output_kind(spec: OperatorSpec, inputs: Sequence[Term]) -> ValueKind:
    """根据运算符契约和输入推导输出值类型。"""
    if isinstance(spec.output_kind, ValueKind):
        return spec.output_kind
    if spec.output_kind == "same" and inputs:
        return inputs[0].value_kind
    raise CompileError(f"Operator {spec.name!r} has invalid output_kind")


def _operator_lookback(spec: OperatorSpec, params: Mapping[str, Any]) -> int:
    """根据运算符契约和参数推导非负日期回看长度。"""
    # 静态值和动态函数统一求值为整数，并包装参数错误。
    try:
        value = (
            spec.date_lookback(dict(params))
            if callable(spec.date_lookback)
            else spec.date_lookback
        )
        value = int(value)
    except Exception as exc:
        raise CompileError(
            f"Cannot infer lookback for operator {spec.name!r}: {exc}"
        ) from exc
    if value < 0:
        raise CompileError(f"Operator {spec.name!r} has negative lookback")
    return value


def _normalize_value(value: Any) -> Any:
    """把语义参数递归规范化为稳定可哈希的基础值。"""
    # 容器递归冻结，NumPy 标量转换为对应 Python 标量。
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, list):
        return tuple(_normalize_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_normalize_value(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (str(key), _normalize_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    raise TypeError(f"Unsupported semantic value {type(value).__name__}")


def _configuration_literal(expr: Expr) -> Any:
    """提取 operator 配置字面量，并支持一元负数字面量。"""
    # 普通字面量直接返回，负号只允许作用于数字字面量。
    if isinstance(expr, LiteralExpr):
        return expr.value
    if (
        isinstance(expr, OperatorExpr)
        and expr.name == "neg"
        and len(expr.args) == 1
        and not expr.params
    ):
        value = _configuration_literal(expr.args[0])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError
        return -value
    raise TypeError
