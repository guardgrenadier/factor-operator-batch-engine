"""Operator 生产尺寸基准。

用法：
    .venv/bin/python benchmarks/operator_benchmark.py --preset daily --days 252
    .venv/bin/python benchmarks/operator_benchmark.py --preset 5min --days 20
    .venv/bin/python benchmarks/operator_benchmark.py --preset 1min --days 20
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import threading
import time
import tracemalloc
from collections.abc import Callable

import numpy as np

from factor_engine.operators.cross_section import cs_zscore, neutralize
from factor_engine.operators.elementwise import greater, where
from factor_engine.operators.timeseries import (
    intraday_by_step_std,
    step_kurtosis,
)


PRESETS = {"daily": 1, "5min": 48, "1min": 237}


def _rss_bytes() -> int:
    """读取当前进程常驻内存（RSS）字节数。"""
    with open("/proc/self/statm", encoding="ascii") as stream:
        return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def _measure(name: str, operation: Callable[[], np.ndarray], phase: str) -> dict:
    """测量单次算子调用的耗时、峰值内存与临时分配。"""
    gc.collect()
    baseline = _rss_bytes()
    peak = [baseline]
    stopped = threading.Event()

    def sample() -> None:
        """后台轮询采样进程峰值 RSS。"""
        while not stopped.wait(0.002):
            peak[0] = max(peak[0], _rss_bytes())

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    tracemalloc.start()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - started
    _, allocation_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stopped.set()
    sampler.join()
    peak[0] = max(peak[0], _rss_bytes())
    rss_delta = max(0, peak[0] - baseline)
    return {
        "operator": name,
        "phase": phase,
        "seconds": round(elapsed, 6),
        "peak_rss_mb": round(rss_delta / 2**20, 2),
        "output_mb": round(result.nbytes / 2**20, 2),
        "temporary_allocation_mb": round(
            max(0, allocation_peak - result.nbytes) / 2**20, 2
        ),
    }


def main() -> None:
    """按预设尺寸构造算子基准并打印 JSON 测量结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=PRESETS, required=True)
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--assets", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    shape = (args.days, args.assets, PRESETS[args.preset])
    rng = np.random.default_rng(args.seed)
    dense = rng.standard_normal(shape)
    nan_heavy = dense.copy()
    nan_heavy[rng.random(shape) < 0.35] = np.nan
    singleton = rng.standard_normal((args.days, args.assets, 1))
    mask = np.where(np.isnan(nan_heavy), np.nan, nan_heavy > 0).astype(np.float64)
    cases = {
        "comparison_dense": lambda: greater(dense, 0.0),
        "where_nan_heavy": lambda: where(mask, nan_heavy, 0.0),
        "cs_zscore_nan_heavy": lambda: cs_zscore(nan_heavy),
        "neutralize_singleton": lambda: neutralize(nan_heavy, singleton),
        "intraday_rolling_std": lambda: intraday_by_step_std(
            nan_heavy, min(5, args.days)
        ),
    }
    if shape[2] >= 4:
        cases["step_kurtosis"] = lambda: step_kurtosis(nan_heavy)

    print(json.dumps({"preset": args.preset, "shape": shape}))
    for name, operation in cases.items():
        print(json.dumps(_measure(name, operation, "first_call")))
        print(json.dumps(_measure(name, operation, "warm_call")))


if __name__ == "__main__":
    main()
