"""定义仅供编译期使用的公共数组算子领域推导规则。"""

from __future__ import annotations

import operator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping

from ..domain import is_intraday_freq

if TYPE_CHECKING:
    from ..model import TermDomain


def numpy_domain(
    domains: tuple[TermDomain | None, ...], _: Mapping[str, Any]
) -> TermDomain | None:
    """按照资产轴和 step 轴的 singleton 规则合并输入 Domain。"""
    # 标量输入没有 Domain，只让实际张量参与坐标推导。
    tensors = _tensors(domains)
    return None if not tensors else _merge_domains(tensors)


def same_asset_domain(
    domains: tuple[TermDomain | None, ...], _: Mapping[str, Any]
) -> TermDomain:
    """要求所有张量输入使用同一个具名资产轴。"""
    tensors = _tensors(domains)
    if not tensors:
        raise _error("Operator requires at least one tensor Domain")
    return _merge_domains(tensors, require_same_asset=True)


def asset_reduce_domain(
    domains: tuple[TermDomain | None, ...], params: Mapping[str, Any]
) -> TermDomain:
    """把具名资产轴归约为匿名 singleton 资产轴。"""
    return replace(same_asset_domain(domains, params), codes=None)


def step_reduce_domain(
    domains: tuple[TermDomain | None, ...], params: Mapping[str, Any]
) -> TermDomain:
    """把完整日内 step 轴归约为日频 singleton。"""
    domain = numpy_domain(domains, params)
    if domain is None:
        raise _error("Operator requires at least one tensor Domain")
    frequency = "1d" if is_intraday_freq(domain.frequency) else domain.frequency
    return replace(domain, frequency=frequency, step_count=1)


def get_step_domain(
    domains: tuple[TermDomain | None, ...], params: Mapping[str, Any]
) -> TermDomain:
    """校验 step 位置并把选中位置保留为 singleton 轴。"""
    domain = _one_tensor(domains)
    _position(params.get("step", 0), domain.step_count, "step")
    return replace(domain, step_count=1)


def select_by_pos_domain(
    domains: tuple[TermDomain | None, ...], params: Mapping[str, Any]
) -> TermDomain:
    """推导保持维度的资产或 step 位置选择结果。"""
    domain = _one_tensor(domains)
    # Runtime 只支持统一的 T×N×S 正轴编号。
    axis = params.get("axis", 1)
    if not params.get("keepdims", False):
        raise _error("select_by_pos requires keepdims=True in batch execution")
    if axis == 0:
        raise _error("select_by_pos cannot select the partitioned date axis")
    # 先按目标轴长度规范化负位置，再更新对应的 Domain 维度。
    length = domain.asset_count if axis == 1 else domain.step_count
    position = _position(params.get("pos"), length, f"axis {axis} position")
    if axis == 2:
        return replace(domain, step_count=1)
    if domain.codes is None:
        raise _error("Cannot select a code from an anonymous asset singleton")
    return replace(domain, codes=(domain.codes[position],))


def slice_step_domain(
    domains: tuple[TermDomain | None, ...], params: Mapping[str, Any]
) -> TermDomain:
    """根据 Python slice 规则推导 step 切片后的长度。"""
    # 将可选边界规范化后，用实际 range 长度更新第三维。
    domain = _one_tensor(domains)
    bounds = (
        _optional_integer(params.get("start"), "start"),
        _optional_integer(params.get("end"), "end"),
    )
    count = len(range(*slice(*bounds).indices(domain.step_count)))
    if count == 0:
        raise _error("slice_step cannot produce an empty step axis")
    return replace(domain, step_count=count)


def lookup_by_col_domain(
    domains: tuple[TermDomain | None, ...], _: Mapping[str, Any]
) -> TermDomain:
    """把源数值投影到 mapping Term 携带的具名资产轴。"""
    # mapping 决定输出资产轴，但时间轴仍需与源数值合并。
    if len(domains) != 2 or any(domain is None for domain in domains):
        raise _error("lookup_by_col requires two tensor Domains")
    source, mapping = domains
    assert source is not None and mapping is not None
    if source.codes is None or mapping.codes is None:
        raise _error("lookup_by_col requires named source and target axes")
    if mapping.step_count != 1:
        raise _error("lookup_by_col mapping must have one step")
    frequency, step_count = _merge_temporal((source, mapping))
    return replace(mapping, frequency=frequency, step_count=step_count)


def _merge_domains(
    domains: tuple[TermDomain, ...], *, require_same_asset: bool = False
) -> TermDomain:
    """合并一组非空张量 Domain，并按策略选择输出资产轴。"""
    # 时间轴规则与资产轴规则彼此独立，先得到共同频率和 step 数。
    frequency, step_count = _merge_temporal(domains)
    candidates = (
        domains
        if require_same_asset
        else tuple(domain for domain in domains if domain.asset_count > 1)
    )
    # 普通算子忽略 singleton 的资产身份；严格算子检查全部具名轴。
    if not candidates:
        base = domains[0]
    else:
        base = candidates[0]
        identity = _asset_identity(base)
        if base.codes is None or any(
            domain.codes is None or _asset_identity(domain) != identity
            for domain in candidates[1:]
        ):
            message = (
                "Operator inputs must share the same full asset axis"
                if require_same_asset
                else "Operator mixes incompatible full asset axes"
            )
            raise _error(message)
    return replace(base, frequency=frequency, step_count=step_count)


def _merge_temporal(domains: tuple[TermDomain, ...]) -> tuple[str, int]:
    """校验日历和频率，并按 singleton 规则合并 step_count。"""
    # 日期轴由任务统一解析，输入仍必须来自同一个交易日历。
    calendars = {domain.calendar for domain in domains}
    if len(calendars) > 1:
        raise _error(f"Operator mixes incompatible calendars: {sorted(calendars)}")
    step_count = _broadcast_steps(domains)
    # 同频率直接合并；跨频率仅允许日频 singleton 进入一个分钟域。
    frequencies = {domain.frequency for domain in domains}
    if len(frequencies) == 1:
        return domains[0].frequency, step_count
    intraday = frequencies - {"1d"}
    if len(intraday) != 1 or any(
        domain.frequency == "1d" and domain.step_count != 1 for domain in domains
    ):
        raise _error(
            "Operator inputs use incompatible frequencies; align them explicitly"
        )
    return next(iter(intraday)), step_count


def _broadcast_steps(domains: tuple[TermDomain, ...]) -> int:
    """返回多个 Domain 在 NumPy singleton 规则下的共同 step 数。"""
    values = tuple(domain.step_count for domain in domains)
    non_singleton = {value for value in values if value != 1}
    if len(non_singleton) > 1:
        raise _error(f"Incompatible step dimensions: {values}")
    return next(iter(non_singleton), 1)


def _asset_identity(domain: TermDomain) -> tuple[Any, ...]:
    """返回完整资产轴参与一致性检查的稳定身份。"""
    return domain.asset_type, domain.codes, domain.axis_fingerprint


def _tensors(domains: tuple[TermDomain | None, ...]) -> tuple[TermDomain, ...]:
    """过滤没有坐标 Domain 的标量输入。"""
    return tuple(domain for domain in domains if domain is not None)


def _one_tensor(domains: tuple[TermDomain | None, ...]) -> TermDomain:
    """提取唯一张量 Domain，并拒绝零个或多个张量输入。"""
    tensors = _tensors(domains)
    if len(tensors) != 1:
        raise _error("Operator requires exactly one tensor Domain")
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
