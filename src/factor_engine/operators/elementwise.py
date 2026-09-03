"""元素级算子：数值算术、比较关系与三值掩码逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import numpy as np
from numba import njit

from ..domain import ValueKind


@dataclass(frozen=True)
class VariadicInput:
    """表示元素类型一致的可变参数输入契约。"""

    kind: ValueKind
    min_count: int = 1


@dataclass(frozen=True)
class OperatorSpec:
    """单个算子的契约：函数、输入输出值类型、Lookback、布局规则与参数校验。"""

    name: str
    func: Callable[..., Any]
    input_kinds: tuple[ValueKind, ...] | VariadicInput
    output_kind: ValueKind | str
    date_lookback: int | Callable[[dict[str, Any]], int] = 0
    layout_rule: Callable[[tuple[Any | None, ...], Mapping[str, Any]], Any] | None = (
        None
    )
    optional_inputs: tuple[tuple[str, ValueKind], ...] = ()
    # 算子业务参数的编译期校验与规范化钩子；返回规范化后的参数字典。
    validate_params: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def add(x, y):
    """逐元素相加。"""
    return x + y


def subtract(x, y):
    """逐元素相减。"""
    return x - y


def multiply(x, y):
    """逐元素相乘。"""
    return x * y


def divide(x, y):
    """逐元素安全相除，并把无穷值转为 nan。"""
    left, right = _broadcast_inputs(x, y)
    return _divide_kernel(left, right)


def neg(x):
    """逐元素取相反数。"""
    return -x


def abs_val(x):
    """逐元素取绝对值。"""
    return np.abs(x)


def _nan_invalid_log_result(result):
    """把标量或数组对数结果中的无穷值统一替换为 NaN。"""
    if np.isscalar(result):
        return np.nan if np.isinf(result) else result
    result[np.isinf(result)] = np.nan
    return result


def ln(x):
    """逐元素取自然对数，并把非法值转为 nan。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.log(x)
    return _nan_invalid_log_result(result)


def log(x):
    """逐元素取自然对数；保留为 ln 的兼容别名。"""
    return ln(x)


def log10(x):
    """逐元素取以 10 为底的对数，并把非法值转为 nan。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.log10(x)
    return _nan_invalid_log_result(result)


def sqrt(x):
    """逐元素取平方根。"""
    with np.errstate(invalid="ignore"):
        return np.sqrt(x)


def sign(x):
    """逐元素取符号（1、0、-1），缺失保持 nan。"""
    return np.sign(x)


def power2(x):
    """逐元素取平方。"""
    return np.square(x)


def power3(x):
    """逐元素取立方。"""
    return np.power(x, 3)


def curt(x):
    """逐元素取立方根。"""
    return np.cbrt(x)


def inv(x):
    """逐元素取倒数，除零或非有限结果转为 nan。"""
    left, right = _broadcast_inputs(1.0, x)
    return _divide_kernel(left, right)


def exp(x):
    """逐元素取自然指数。"""
    with np.errstate(over="ignore", invalid="ignore"):
        return np.exp(x)


def power(x, y):
    """逐元素取幂，非法结果（如负数的分数次幂）转为 nan。"""
    with np.errstate(invalid="ignore", over="ignore"):
        return np.power(x, y)


def protected_sqrt(x):
    """逐元素带符号平方根：sign(x) * sqrt(|x|)。"""
    with np.errstate(invalid="ignore"):
        return np.sqrt(np.abs(x)) * np.sign(x)


def protected_log(x):
    """逐元素带符号对数：sign(x) * log(|x| + 1)。"""
    with np.errstate(invalid="ignore", divide="ignore"):
        return _nan_invalid_log_result(np.log(np.abs(x) + 1.0) * np.sign(x))


def sin(x):
    """逐元素取正弦。"""
    return np.sin(x)


def cos(x):
    """逐元素取余弦。"""
    return np.cos(x)


def tan(x):
    """逐元素取正切。"""
    with np.errstate(invalid="ignore"):
        return np.tan(x)


def sigmoid(x):
    """逐元素取 sigmoid：1 / (1 + exp(-x))，大绝对值自然饱和到 0/1。"""
    with np.errstate(over="ignore", invalid="ignore"):
        left, right = _broadcast_inputs(1.0, 1.0 + np.exp(-x))
        return _divide_kernel(left, right)


def hardsigmoid(x):
    """逐元素取分段线性近似 sigmoid：clip((x + 1) / 2, 0, 1)。"""
    return np.clip((x + 1.0) / 2.0, 0.0, 1.0)


def leakyrelu(x, alpha=0.1):
    """逐元素 Leaky ReLU：x > 0 时为 x，否则为 alpha * x。"""
    return np.where(x > 0, x, alpha * x)


def gelu(x):
    """逐元素取 tanh 近似的 GELU。"""
    return (
        x
        * 0.5
        * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * np.power(x, 3))))
    )


def series_min(x, y):
    """逐元素取两者较小值，任一输入缺失输出缺失。"""
    return np.minimum(x, y)


def series_max(x, y):
    """逐元素取两者较大值，任一输入缺失输出缺失。"""
    return np.maximum(x, y)


def one(x):
    """逐元素常数 1，输入缺失位置保持缺失。"""
    return np.where(np.isnan(x), np.nan, 1.0)


def if_then_else(x, y, o1, o2):
    """按 x >= y 的三值比较结果选择 o1 或 o2，条件缺失输出缺失。"""
    return where(greater_equal(x, y), o1, o2)


def _compare(x, y, mode):
    """单次扫描比较，并在任一输入 Missing 时保留 Missing。"""
    left, right = _broadcast_inputs(x, y)
    return _compare_kernel(left, right, mode)


def greater_equal(x, y):
    """逐元素判断大于等于。"""
    return _compare(x, y, 0)


def greater(x, y):
    """逐元素判断大于。"""
    return _compare(x, y, 1)


def less_equal(x, y):
    """逐元素判断小于等于。"""
    return _compare(x, y, 2)


def less(x, y):
    """逐元素判断小于。"""
    return _compare(x, y, 3)


def equal(x, y):
    """逐元素判断相等。"""
    return _compare(x, y, 4)


def not_equal(x, y):
    """逐元素判断不相等。"""
    return _compare(x, y, 5)


def where(mask, x, y=np.nan):
    """按三值 mask 选择；Missing 条件产生 Missing。"""
    condition, left, right = _broadcast_inputs(mask, x, y)
    return _where_kernel(condition, left, right)


def apply_mask(x, mask):
    """只保留 mask 为 True 的值，False 或 Missing 均输出 Missing。"""
    values, condition = _broadcast_inputs(x, mask)
    return _apply_mask_kernel(values, condition)


def mask_and(*masks):
    """按照 False 优先的三值真值表计算逻辑与。"""
    arrays = _broadcast_inputs(*masks)
    # 单输入直接返回；多个输入先用 pair kernel 生成结果再逐个折叠。
    if len(arrays) == 1:
        return arrays[0]
    out = _mask_and_pair_kernel(arrays[0], arrays[1])
    for array in arrays[2:]:
        _mask_and_kernel(out, array)
    return out


def mask_or(*masks):
    """按照 True 优先的三值真值表计算逻辑或。"""
    arrays = _broadcast_inputs(*masks)
    # 单输入直接返回；多个输入先用 pair kernel 生成结果再逐个折叠。
    if len(arrays) == 1:
        return arrays[0]
    out = _mask_or_pair_kernel(arrays[0], arrays[1])
    for array in arrays[2:]:
        _mask_or_kernel(out, array)
    return out


def mask_not(mask):
    """按照三值真值表计算逻辑非。"""
    return _mask_not_kernel(_broadcast_inputs(mask)[0])


def _broadcast_inputs(*values):
    """只生成广播 view，并明确标记为不可写输入。"""
    arrays = np.broadcast_arrays(*values)
    for array in arrays:
        array.setflags(write=False)
    return arrays


@njit(cache=True)
def _divide_kernel(left, right):
    """逐元素安全相除，除零或非有限结果转为 Missing。"""
    out = np.empty(left.shape, dtype=np.float64)
    # 单次扫描除法，除数为零的位置输出 Missing。
    for index in np.ndindex(left.shape):
        denominator = right[index]
        value = left[index] / denominator if denominator != 0.0 else np.nan
        out[index] = value if np.isfinite(value) else np.nan
    return out


@njit(cache=True)
def _compare_kernel(left, right, mode):
    """逐元素比较，mode 选择比较关系，任一输入缺失输出 Missing。"""
    out = np.empty(left.shape, dtype=np.float64)
    # 按 mode 选择比较关系，任一输入为 nan 时输出 Missing。
    for index in np.ndindex(left.shape):
        x = left[index]
        y = right[index]
        if np.isnan(x) or np.isnan(y):
            out[index] = np.nan
        elif mode == 0:
            out[index] = x >= y
        elif mode == 1:
            out[index] = x > y
        elif mode == 2:
            out[index] = x <= y
        elif mode == 3:
            out[index] = x < y
        elif mode == 4:
            out[index] = x == y
        else:
            out[index] = x != y
    return out


@njit(cache=True)
def _where_kernel(mask, left, right):
    """三值条件选择；Missing 条件输出 Missing。"""
    out = np.empty(mask.shape, dtype=np.float64)
    # 按条件选取左右分支；Missing 条件直接输出 Missing。
    for index in np.ndindex(mask.shape):
        condition = mask[index]
        if np.isnan(condition):
            out[index] = np.nan
        elif condition == 1.0:
            out[index] = left[index]
        else:
            out[index] = right[index]
    return out


@njit(cache=True)
def _apply_mask_kernel(values, mask):
    """只保留 mask 为 True 的位置，其余输出 Missing。"""
    out = np.empty(mask.shape, dtype=np.float64)
    # mask 不为 1 的位置统一输出 Missing。
    for index in np.ndindex(mask.shape):
        out[index] = values[index] if mask[index] == 1.0 else np.nan
    return out


@njit(cache=True)
def _mask_and_pair_kernel(left, right):
    """生成前两个 mask 的三值逻辑与结果。"""
    out = np.empty(left.shape, dtype=np.float64)
    # False 优先：任一为 0 输出 0，其余按三值真值表。
    for index in np.ndindex(out.shape):
        left_value = left[index]
        right_value = right[index]
        if left_value == 0.0 or right_value == 0.0:
            out[index] = 0.0
        elif np.isnan(left_value) or np.isnan(right_value):
            out[index] = np.nan
        else:
            out[index] = 1.0
    return out


@njit(cache=True)
def _mask_and_kernel(out, right):
    """把后续 mask 的三值逻辑与就地折叠到已有结果。"""
    # 按同样的 False 优先真值表重复折叠。
    for index in np.ndindex(out.shape):
        left_value = out[index]
        right_value = right[index]
        if left_value == 0.0 or right_value == 0.0:
            out[index] = 0.0
        elif np.isnan(left_value) or np.isnan(right_value):
            out[index] = np.nan
        else:
            out[index] = 1.0


@njit(cache=True)
def _mask_or_pair_kernel(left, right):
    """生成前两个 mask 的三值逻辑或结果。"""
    out = np.empty(left.shape, dtype=np.float64)
    # True 优先：任一为 1 输出 1，其余按三值真值表。
    for index in np.ndindex(out.shape):
        left_value = left[index]
        right_value = right[index]
        if left_value == 1.0 or right_value == 1.0:
            out[index] = 1.0
        elif np.isnan(left_value) or np.isnan(right_value):
            out[index] = np.nan
        else:
            out[index] = 0.0
    return out


@njit(cache=True)
def _mask_or_kernel(out, right):
    """把后续 mask 的三值逻辑或就地折叠到已有结果。"""
    # 按同样的 True 优先真值表重复折叠。
    for index in np.ndindex(out.shape):
        left_value = out[index]
        right_value = right[index]
        if left_value == 1.0 or right_value == 1.0:
            out[index] = 1.0
        elif np.isnan(left_value) or np.isnan(right_value):
            out[index] = np.nan
        else:
            out[index] = 0.0


@njit(cache=True)
def _mask_not_kernel(mask):
    """逐元素三值逻辑非。"""
    out = np.empty(mask.shape, dtype=np.float64)
    # 交换 True 与 False，Missing 原样保留。
    for index in np.ndindex(mask.shape):
        value = mask[index]
        out[index] = np.nan if np.isnan(value) else 1.0 - value
    return out
