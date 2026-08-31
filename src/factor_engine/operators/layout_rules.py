"""普通 Operator 的纯 ArrayLayout 推导规则。"""

from __future__ import annotations

import operator
from dataclasses import replace
from typing import Any, Mapping

from ..domain import ArrayLayout


def numpy_layout(
    layouts: tuple[ArrayLayout, ...], _: Mapping[str, Any]
) -> ArrayLayout:
    """按 NumPy singleton 规则合并 N/S，忽略所有业务坐标。"""

    tensors = tuple(layout for layout in layouts if not layout.scalar)
    if not tensors:
        return ArrayLayout(True)
    return ArrayLayout(
        False,
        _broadcast(tuple(layout.asset_count for layout in tensors), "asset"),
        _broadcast(tuple(layout.step_count for layout in tensors), "step"),
    )


def asset_reduce_layout(
    layouts: tuple[ArrayLayout, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """先广播输入，再把 N 归约为 singleton。"""

    return replace(_tensor_result(layouts, params), asset_count=1)


def step_reduce_layout(
    layouts: tuple[ArrayLayout, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """先广播输入，再把 S 归约为 singleton。"""

    return replace(_tensor_result(layouts, params), step_count=1)


def get_step_layout(
    layouts: tuple[ArrayLayout, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """校验 step 位置并保留 singleton S。"""

    layout = _one_tensor(layouts)
    _position(params.get("step", 0), layout.step_count, "step")
    return replace(layout, step_count=1)


def select_by_pos_layout(
    layouts: tuple[ArrayLayout, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """校验 N/S 位置选择并把目标轴变为 singleton。"""

    layout = _one_tensor(layouts)
    axis = params.get("axis", 1)
    if not params.get("keepdims", False):
        raise _error("select_by_pos requires keepdims=True in batch execution")
    if axis == 0:
        raise _error("select_by_pos cannot select the partitioned date axis")
    length = layout.asset_count if axis == 1 else layout.step_count
    _position(params.get("pos"), length, f"axis {axis} position")
    return replace(
        layout,
        asset_count=1 if axis == 1 else layout.asset_count,
        step_count=1 if axis == 2 else layout.step_count,
    )


def slice_step_layout(
    layouts: tuple[ArrayLayout, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    """按 Python slice 规则推导新的 S。"""

    layout = _one_tensor(layouts)
    bounds = (
        _optional_integer(params.get("start"), "start"),
        _optional_integer(params.get("end"), "end"),
    )
    count = len(range(*slice(*bounds).indices(layout.step_count)))
    if count == 0:
        raise _error("slice_step cannot produce an empty step axis")
    return replace(layout, step_count=count)


def _tensor_result(
    layouts: tuple[ArrayLayout, ...], params: Mapping[str, Any]
) -> ArrayLayout:
    result = numpy_layout(layouts, params)
    if result.scalar:
        raise _error("Operator requires at least one tensor input")
    return result


def _one_tensor(layouts: tuple[ArrayLayout, ...]) -> ArrayLayout:
    tensors = tuple(layout for layout in layouts if not layout.scalar)
    if len(tensors) != 1:
        raise _error("Operator requires exactly one tensor input")
    return tensors[0]


def _broadcast(values: tuple[int, ...], axis: str) -> int:
    non_singleton = {value for value in values if value != 1}
    if len(non_singleton) > 1:
        raise _error(f"Incompatible {axis} dimensions: {values}")
    return next(iter(non_singleton), 1)


def _position(value: Any, length: int, name: str) -> int:
    position = _integer(value, name)
    if not -length <= position < length:
        raise _error(f"{name} {position} is outside an axis of length {length}")
    return position % length


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise _error(f"{name} must be an integer")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise _error(f"{name} must be an integer") from exc


def _optional_integer(value: Any, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _error(message: str) -> Exception:
    """延迟构造 DomainError，避免 formula/operator/model 初始化循环。"""

    from ..model import DomainError

    return DomainError(message)
