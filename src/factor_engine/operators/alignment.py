"""轴对齐函数：资产轴映射、位置选择与广播。"""

from __future__ import annotations

import numpy as np
from numba import njit


def lookup_by_col(source_values, source_col):
    """按列位置把源资产数组映射到目标资产轴。"""
    return _lookup_by_col_kernel(source_values, source_col)


def select_by_pos(x, pos, axis=1, keepdims=False):
    """沿指定轴按位置选取数据。"""
    # 不保留维度时直接委托 NumPy，保留时构造长度为一的切片。
    if not keepdims:
        return np.take(x, pos, axis=axis)
    index = [slice(None)] * x.ndim
    index[axis] = slice(pos, pos + 1)
    return x[tuple(index)]


def broadcast_ts(x, n_assets):
    """把时间序列广播到指定资产数。"""
    return np.broadcast_to(x, (x.shape[0], n_assets, x.shape[2]))


def broadcast_to_steps(x, n_steps):
    """把单 step 特征广播到指定 step 数。"""
    return np.broadcast_to(x, (x.shape[0], x.shape[1], n_steps))


@njit(cache=True)
def _lookup_by_col_kernel(source, source_col):
    """按列位置码把源资产映射到目标资产轴，无效码输出 Missing。"""
    out = np.empty(
        (source.shape[0], source_col.shape[1], source.shape[2]), dtype=np.float64
    )
    # 逐目标资产按码值查源列，缺失或越界码输出 Missing。
    for t in range(source.shape[0]):
        for n in range(source_col.shape[1]):
            code = source_col[t, n, 0]
            valid = np.isfinite(code) and 0 <= code < source.shape[1]
            column = int(code) if valid else 0
            for s in range(source.shape[2]):
                out[t, n, s] = source[t, column, s] if valid else np.nan
    return out
