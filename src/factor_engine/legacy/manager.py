"""旧版实现：编排特征依赖递归计算与可选落盘的管理器。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from .data.router import DataRouter
from .engine import (
    Calculator,
    Expr,
    FeatureExpr,
    OpExpr,
    Planner,
    SourceExpr,
    _expr_from_dict,
    _expr_to_dict,
    _mask_expr,
    infer_date_overlap,
)
from .registry import FeatureRegistry
from .data.model import (
    CalculationResult,
    ExecutionRequest,
    ExecutionScope,
    FeatureArray,
    FeatureDef,
    SourceSpec,
    parse_feature_key,
)
from .data.sources import fundamental_name
from .data.store import FeatureStore


class FeatureManager:
    """编排已注册特征的计算与可选落盘。"""

    def __init__(
        self,
        store: str | Path | FeatureStore,
        *,
        data_router: DataRouter | None = None,
        definitions_dir: str | Path | None = None,
    ):
        """初始化特征存储、数据路由和定义注册表。"""
        # 统一外部路径与已构造对象，建立管理器依赖。
        self.store = store if isinstance(store, FeatureStore) else FeatureStore(store)
        self.data_router = data_router or DataRouter()
        root = (
            Path(definitions_dir)
            if definitions_dir is not None
            else self.store.root / "feature_defs"
        )
        self.registry = FeatureRegistry(root)

    def execute(self, request: ExecutionRequest) -> CalculationResult | None:
        """执行单目标请求，并由 Manager 决定是否写入 FeatureStore。"""
        # 分块请求使用带重叠窗口的独立执行路径。
        if request.chunk_size is not None:
            return self._execute_chunked(request)

        # 非分块请求递归计算依赖，并按需物化最终结果。
        feature_def = self.registry.resolve(request.target)
        calculator = Calculator(self.store, data_router=self.data_router)
        try:
            result = self._calculate_with(
                feature_def.key,
                calculator=calculator,
                visiting=set(),
            )
            if request.materialize:
                self.store.write_feature(
                    self._as_feature_array(result, feature_def),
                    overwrite=request.overwrite,
                )
            return result if request.return_array else None
        finally:
            self.data_router.clear_cache()

    def _calculate_with(
        self,
        key: str,
        *,
        calculator: Calculator,
        visiting: set[str],
        scope: ExecutionScope | None = None,
    ) -> CalculationResult:
        """递归计算指定特征并复用当前计算器中的运行时缓存。"""
        feature_def = self.registry.get(key)
        # 优先复用同一执行过程已计算的特征，并检测依赖环。
        cached = calculator.runtime_features.get(feature_def.key)
        if cached is not None:
            return cached
        if feature_def.key in visiting:
            chain = " -> ".join((*visiting, feature_def.key))
            raise ValueError(f"Circular feature dependency detected: {chain}")

        visiting.add(feature_def.key)
        try:
            # 先补齐尚未落盘的注册依赖，再计算当前特征。
            self._calculate_dependencies(
                feature_def,
                calculator=calculator,
                visiting=visiting,
                scope=scope,
            )
            return calculator.calculate(
                _expr_from_dict(feature_def.formula),
                output=feature_def.key,
                input_mask=self._mask_for(feature_def.input_mask),
                sample_mask=self._mask_for(feature_def.sample_mask),
                output_mask=self._mask_for(feature_def.output_mask),
                delay_lf=feature_def.delay_lf,
                delay_dict=feature_def.delay_dict,
                feature_def=feature_def,
                scope=scope,
            )
        finally:
            visiting.remove(feature_def.key)

    def _calculate_dependencies(
        self,
        feature_def: FeatureDef,
        *,
        calculator: Calculator,
        visiting: set[str],
        scope: ExecutionScope | None,
    ) -> None:
        """计算当前定义依赖且尚未落盘的注册特征。"""
        # 已计算、自依赖或已落盘的输入无需递归计算。
        for dependency in feature_def.dependencies:
            if (
                dependency == feature_def.key
                or dependency in calculator.runtime_features
            ):
                continue
            if self.registry.contains(dependency) and not self.store.has_feature(
                dependency
            ):
                self._calculate_with(
                    dependency,
                    calculator=calculator,
                    visiting=visiting,
                    scope=scope,
                )

    def _execute_chunked(self, request: ExecutionRequest) -> CalculationResult | None:
        """按日期分块计算特征并将各块写入同一暂存目录。"""
        # 自动推导窗口重叠长度，也允许调用方显式覆盖。
        feature_def = self.registry.resolve(request.target)
        overlap = (
            self._infer_overlap(feature_def)
            if request.overlap is None
            else int(request.overlap)
        )
        dates = tuple(str(date) for date in self.store.get_dates())
        base_dir: Path | None = None
        dtype = "float64"
        missing_value: Any = np.nan
        try:
            # 先创建暂存目录，再逐块读取含重叠区间的数据并写入有效区间。
            base_dir = self.store.begin_feature_write(
                feature_def, overwrite=request.overwrite
            )
            for chunk_id, write_start in enumerate(
                range(0, len(dates), int(request.chunk_size))
            ):
                write_dates = dates[write_start : write_start + int(request.chunk_size)]
                read_start = max(0, write_start - overlap)
                read_dates = dates[read_start : write_start + int(request.chunk_size)]
                scope = ExecutionScope(
                    read_dates=read_dates, write_dates=write_dates, chunk_id=chunk_id
                )
                calculator = Calculator(self.store, data_router=self.data_router)
                try:
                    # 每个分块使用独立计算器，避免跨块缓存污染。
                    result = self._calculate_with(
                        feature_def.key,
                        calculator=calculator,
                        visiting=set(),
                        scope=scope,
                    )
                    dtype = str(result.values.dtype)
                    missing_value = result.missing_value
                    self.store.write_feature_chunk(
                        self._as_feature_array(result, feature_def),
                        scope,
                        base_dir=base_dir,
                    )
                finally:
                    self.data_router.clear_cache()
            # 所有分块完成后原子发布元数据与数据目录。
            self.store.finalize_feature_write(
                feature_def.key,
                feature_def,
                dtype=dtype,
                missing_value=missing_value,
                metadata={
                    "chunked": True,
                    "chunk_size": int(request.chunk_size),
                    "overlap": overlap,
                },
                base_dir=base_dir,
            )
            base_dir = None
            if not request.return_array:
                return None
            # 调用方需要数组时，从已发布结果重新装载完整特征。
            feature = self.store.load_feature(feature_def.key)
            return CalculationResult(
                key=feature.key,
                values=feature.values,
                space=feature.space,
                missing_value=feature.missing_value,
            )
        except Exception:
            # 任一分块失败都清理未发布的暂存结果。
            if base_dir is not None:
                self.store.abort_feature_write(base_dir)
            self.data_router.clear_cache()
            raise

    def _infer_overlap(
        self, feature_def: FeatureDef, visiting: set[str] | None = None
    ) -> int:
        """递归推导特征及其未落盘依赖所需的日期重叠长度。"""
        # 递归路径用于阻止定义依赖形成闭环。
        visiting = set() if visiting is None else visiting
        if feature_def.key in visiting:
            chain = " -> ".join((*visiting, feature_def.key))
            raise ValueError(
                f"Circular feature dependency detected while inferring overlap: {chain}"
            )
        visiting.add(feature_def.key)
        # 先规划当前表达式，获得算子自身所需的历史窗口。
        planner = Planner(
            self.store,
            aliases={},
            delay_lf=feature_def.delay_lf,
            delay_dict=feature_def.delay_dict,
        )
        planned = planner.plan(
            _expr_from_dict(feature_def.formula),
            output_key=feature_def.key,
            input_mask=self._mask_for(feature_def.input_mask),
            sample_mask=self._mask_for(feature_def.sample_mask),
            output_mask=self._mask_for(feature_def.output_mask),
        )
        # 未落盘依赖的窗口长度需要递归计入总重叠量。
        dependency_overlap = 0
        for dependency in feature_def.dependencies:
            if (
                dependency != feature_def.key
                and self.registry.contains(dependency)
                and not self.store.has_feature(dependency)
            ):
                dependency_overlap = max(
                    dependency_overlap,
                    self._infer_overlap(
                        self.registry.get(dependency), visiting=visiting
                    ),
                )
        visiting.remove(feature_def.key)
        return dependency_overlap + infer_date_overlap(planned)

    @staticmethod
    def _mask_for(mask: Any | None) -> Expr | None:
        """将空值、序列化表达式或掩码简写统一转换为表达式。"""
        if mask is None:
            return None
        if isinstance(mask, dict):
            return _expr_from_dict(mask)
        return _mask_expr(mask)

    @staticmethod
    def _as_feature_array(
        result: CalculationResult, feature_def: FeatureDef
    ) -> FeatureArray:
        """将计算结果封装为可由特征存储写入的数组对象。"""
        # 复制诊断信息作为持久化元数据，避免后续修改原结果。
        return FeatureArray(
            key=result.key,
            values=result.values,
            space=result.space,
            feature_def=feature_def,
            missing_value=result.missing_value,
            metadata=dict(result.diagnostics),
        )


def get_lf(
    field: str,
    *,
    asset: str = "stk",
    freq: str = "1d",
    name: str | None = None,
    alias: str | None = None,
    if_adj: bool = False,
    if_sus: bool = False,
) -> FeatureDef:
    """生成日频叶子或日频公式的 FeatureDef。"""
    # 从原始字段构造叶子表达式，并按选项附加复权与停牌掩码。
    base_key = f"{asset}.{freq}.{field}"
    expr: Expr = FeatureExpr(base_key)
    out_name = name or field
    if if_adj:
        adj_key = f"{asset}.{freq}.adj_factor"
        expr = OpExpr("multiply", (expr, FeatureExpr(adj_key)), {})
        out_name += "_Adj" if name is None else ""
    if if_sus:
        sus_key = f"{asset}.{freq}.IfSuspended"
        expr = OpExpr(
            "apply_mask", (expr, OpExpr("mask_not", (FeatureExpr(sus_key),), {})), {}
        )
        out_name += "_Sus" if name is None else ""
    key = f"{asset}.{freq}.{out_name}"
    fk = parse_feature_key(key)
    return FeatureDef(
        key=fk.key,
        asset=fk.asset,
        freq=fk.freq,
        name=fk.name,
        alias=alias,
        formula=_expr_to_dict(expr),
        params={"field": field, "if_adj": if_adj, "if_sus": if_sus},
        metadata={"helper": "get_lf"},
    )


def get_hf(
    field: str,
    *,
    asset: str = "stk",
    freq: str = "1min",
    name: str | None = None,
    alias: str | None = None,
) -> FeatureDef:
    """生成分钟叶子的 FeatureDef。"""
    raw_key = f"{asset}.{freq}.{field}"
    out_name = name or field
    return FeatureDef(
        key=f"{asset}.{freq}.{out_name}",
        asset=asset,
        freq=freq,
        name=out_name,
        alias=alias,
        formula=_expr_to_dict(FeatureExpr(raw_key)),
        params={"field": field},
        metadata={"helper": "get_hf"},
    )


def get_fund(
    field: str,
    *,
    column_name: str,
    quarters: int = 1,
    data_code: Any | None = None,
    publ_date_limit: int = -180,
    asset: str = "stk",
    freq: str = "1d",
    name: str | None = None,
    alias: str | None = None,
) -> FeatureDef:
    """生成包含显式 fundamental SourceExpr 的 FeatureDef。"""
    # 参数组合先编码为稳定原始数据源名称。
    raw_name = fundamental_name(
        field, column_name, quarters, data_code, publ_date_limit
    )
    raw_key = f"{asset}.{freq}.{raw_name}"
    out_name = name or raw_name
    # 兼容从旧式表名中提取 ItemCode，同时保留显式配置优先级。
    resolved_data_code = data_code
    if resolved_data_code is None:
        match = re.fullmatch(r"Fundamental_Item(\d+)", field)
        if match:
            resolved_data_code = int(match.group(1))
    # 外部来源读取参数全部固化进 SourceExpr，执行期无需业务推断。
    spec = SourceSpec.from_key(
        raw_key,
        source="Fundamental",
        field=field,
        params={
            "column_name": column_name,
            "quarters": int(quarters),
            "data_code": resolved_data_code,
            "publ_date_limit": int(publ_date_limit),
        },
    )
    return FeatureDef(
        key=f"{asset}.{freq}.{out_name}",
        asset=asset,
        freq=freq,
        name=out_name,
        alias=alias,
        formula=_expr_to_dict(SourceExpr(spec)),
        params={
            "field": field,
            "column_name": column_name,
            "quarters": quarters,
            "data_code": data_code,
            "publ_date_limit": publ_date_limit,
        },
        steps=int(quarters),
        metadata={"helper": "get_fund"},
    )
