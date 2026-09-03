"""定义仅供编译期使用的公共数组算子布局推导规则。

普通算子只处理 ArrayLayout 的物理形状：N、S 分别按 NumPy 广播规则合并，
不比较资产类型、代码及顺序、frequency、calendar 或 axis fingerprint。
布局上的 asset_type / frequency 是无歧义时传播的溯源提示，只服务于
shape 失败诊断与显式坐标变换算子的专属 lowering。
"""

from __future__ import annotations

import operator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping

from ..domain import is_intraday_freq

if TYPE_CHECKING:
    from ..model import ArrayLayout


def broadcast_layout(
    layouts: tuple[ArrayLayout | None, ...], _: Mapping[str, Any]
) -> ArrayLayout | None:
    """按 NumPy 广播规则合并输入布局的 N 与 S，并传播无歧义的溯源提示。"""
    # 标量输入没有布局，只让实际张量参与形状推导。
    tensors = _tensors(layouts)
    if not tensors:
        return None
    return _broadcast(tensors)


def asset_reduce_layout(
    layouts: tuple[ArrayLayout | None, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """把资产轴归约为匿名 singleton 资产轴。"""
    layout = broadcast_layout(layouts, params)
    if layout is None:
        raise _error("Operator requires at least one tensor layout")
    return replace(layout, asset_count=1, asset_type=None)


def step_reduce_layout(
    layouts: tuple[ArrayLayout | None, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """把完整日内 step 轴归约为日频 singleton。"""
    layout = broadcast_layout(layouts, params)
    if layout is None:
        raise _error("Operator requires at least one tensor layout")
    hint = layout.frequency
    if hint is not None and is_intraday_freq(hint):
        hint = "1d"
    return replace(layout, step_count=1, frequency=hint)


def get_step_layout(
    layouts: tuple[ArrayLayout | None, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """校验 step 位置并把选中位置保留为 singleton 轴。"""
    layout = _one_tensor(layouts)
    _position(params.get("step", 0), layout.step_count, "step")
    return replace(layout, step_count=1)


def select_by_pos_layout(
    layouts: tuple[ArrayLayout | None, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """推导保持维度的资产或 step 位置选择结果。"""
    layout = _one_tensor(layouts)
    # Runtime 只支持统一的 T×N×S 正轴编号。
    axis = params.get("axis", 1)
    if not params.get("keepdims", False):
        raise _error("select_by_pos requires keepdims=True in batch execution")
    if axis == 0:
        raise _error("select_by_pos cannot select the partitioned date axis")
    # 先按目标轴长度规范化负位置，再更新对应的布局维度。
    length = layout.asset_count if axis == 1 else layout.step_count
    _position(params.get("pos"), length, f"axis {axis} position")
    if axis == 2:
        return replace(layout, step_count=1)
    # 资产选择产生匿名 singleton，资产身份不再随布局传播。
    return replace(layout, asset_count=1, asset_type=None)


def slice_step_layout(
    layouts: tuple[ArrayLayout | None, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """根据 Python slice 规则推导 step 切片后的长度。"""
    # 将可选边界规范化后，用实际 range 长度更新第三维。
    layout = _one_tensor(layouts)
    bounds = (
        _optional_integer(params.get("start"), "start"),
        _optional_integer(params.get("end"), "end"),
    )
    count = len(range(*slice(*bounds).indices(layout.step_count)))
    if count == 0:
        raise _error("slice_step cannot produce an empty step axis")
    return replace(layout, step_count=count)


def location_layout(
    layouts: tuple[ArrayLayout | None, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """位置序号只允许在资产或 step 轴上生成，日期轴因分区执行被拒绝。"""
    layout = broadcast_layout(layouts, params)
    if layout is None:
        raise _error("location requires at least one tensor layout")
    if params.get("axis", 1) == 0:
        raise _error("location cannot index the partitioned date axis")
    return layout


def lookup_by_col_layout(
    layouts: tuple[ArrayLayout | None, ...], _: Mapping[str, Any]
) -> ArrayLayout:
    """把源数值投影到 mapping 布局携带的资产轴。"""
    # mapping 决定输出资产轴，step 轴仍按广播规则与源数值合并。
    if len(layouts) != 2 or any(layout is None for layout in layouts):
        raise _error("lookup_by_col requires two tensor layouts")
    source, mapping = layouts
    assert source is not None and mapping is not None
    if mapping.step_count != 1:
        raise _error("lookup_by_col mapping must have one step")
    step_count = _broadcast_dim(source.step_count, 1, "step", source, mapping)
    return replace(
        source,
        asset_count=mapping.asset_count,
        step_count=step_count,
        asset_type=mapping.asset_type,
    )


def _broadcast(layouts: tuple[ArrayLayout, ...]) -> ArrayLayout:
    """按 NumPy 广播规则合并一组非空布局并传播溯源提示。"""
    base = layouts[0]
    for other in layouts[1:]:
        asset_count = _broadcast_dim(
            base.asset_count, other.asset_count, "asset", base, other
        )
        step_count = _broadcast_dim(
            base.step_count, other.step_count, "step", base, other
        )
        base = replace(base, asset_count=asset_count, step_count=step_count)
    return replace(
        base,
        asset_type=_unique_hint(layouts, "asset_type", "asset_count"),
        frequency=_unique_hint(layouts, "frequency", "step_count"),
    )


def _broadcast_dim(
    left: int,
    right: int,
    axis: str,
    left_layout: ArrayLayout,
    right_layout: ArrayLayout,
) -> int:
    """合并单个维度：相等或一侧为 1 时广播，否则报错并增强资产维诊断。"""
    if left == right:
        return left
    if left == 1:
        return right
    if right == 1:
        return left
    if axis == "asset":
        raise _error(_asset_mismatch_message(left_layout, right_layout))
    raise _error(f"Incompatible step dimensions: ({left}, {right})")


def _asset_mismatch_message(left: ArrayLayout, right: ArrayLayout) -> str:
    """为资产维不可广播构造诊断，能确定唯一资产来源时附带资产类型。"""
    # 两侧都有唯一、无歧义的资产类型来源时给出跨资产误用提示。
    if (
        left.asset_type is not None
        and right.asset_type is not None
        and left.asset_type != right.asset_type
    ):
        return (
            f"Asset dimension mismatch: {left.asset_type}(N={left.asset_count}) "
            f"cannot broadcast with {right.asset_type}(N={right.asset_count}); "
            "use an explicit mapping, selection, or reduction operator"
        )
    return f"Incompatible asset dimensions: ({left.asset_count}, {right.asset_count})"


def _unique_hint(
    layouts: tuple[ArrayLayout, ...], field: str, size_field: str
) -> str | None:
    """传播唯一无歧义的溯源提示；优先取在该轴上非 singleton 的输入。"""
    full = {
        getattr(layout, field)
        for layout in layouts
        if getattr(layout, size_field) > 1 and getattr(layout, field) is not None
    }
    if len(full) == 1:
        return next(iter(full))
    if full:
        return None
    hints = {getattr(layout, field) for layout in layouts} - {None}
    return next(iter(hints)) if len(hints) == 1 else None


def _tensors(layouts: tuple[ArrayLayout | None, ...]) -> tuple[ArrayLayout, ...]:
    """过滤没有坐标布局的标量输入。"""
    return tuple(layout for layout in layouts if layout is not None)


def _one_tensor(layouts: tuple[ArrayLayout | None, ...]) -> ArrayLayout:
    """提取唯一张量布局，并拒绝零个或多个张量输入。"""
    tensors = _tensors(layouts)
    if len(tensors) != 1:
        raise _error("Operator requires exactly one tensor layout")
    return tensors[0]


def _position(value: Any, length: int, name: str) -> int:
    """校验轴位置并转换为非负位置。"""
    position = _integer(value, name)
    if not -length <= position < length:
        raise _error(f"{name} {position} is outside an axis of length {length}")
    return position % length


def _integer(value: Any, name: str) -> int:
    """按照索引协议校验整数配置参数。"""
    if isinstance(value, bool):
        raise _error(f"{name} must be an integer")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise _error(f"{name} must be an integer") from exc


def _optional_integer(value: Any, name: str) -> int | None:
    """校验允许为 None 的整数配置参数。"""
    return None if value is None else _integer(value, name)


def _error(message: str) -> Exception:
    """延迟构造 DomainError 以避免模块初始化循环。"""
    from ..model import DomainError

    return DomainError(message)
