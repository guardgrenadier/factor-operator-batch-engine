"""对比完整 DataFrame 物质化与 Arrow 分批直接散点写入的耗时和内存。

在 Linux/macOS 上运行：
    uv run python benchmarks/profile_minute_arrow.py --rows 5000000
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def _make_fixture(path: Path, rows: int) -> None:
    """生成指定行数的分钟级 parquet 基准数据。"""
    row = np.arange(rows, dtype=np.int64)
    table = pa.table(
        {
            "date_idx": row % 20,
            "asset_idx": (row // 20) % 2000,
            "step_idx": (row // 40_000) % 237,
            "close": row.astype(np.float64),
            "volume": row.astype(np.float64) * 10,
        }
    )
    pq.write_table(table, path)


def _profile(mode: str, path: Path, batch_rows: int) -> dict[str, float | str]:
    """按旧版 DataFrame 或 Arrow 分批模式读取并返回耗时与峰值内存。"""
    sql = f"SELECT * FROM read_parquet('{path.as_posix()}')"
    shape = (20, 2000, 237)
    started = perf_counter()
    connection = duckdb.connect()
    try:
        close = np.full(shape, np.nan)
        volume = np.full(shape, np.nan)
        if mode == "old":
            rows = connection.execute(sql).df()
            if rows.duplicated(["date_idx", "asset_idx", "step_idx"]).any():
                raise ValueError("duplicate coordinates")
            indices = tuple(
                rows[name].to_numpy(dtype=np.intp)
                for name in ("date_idx", "asset_idx", "step_idx")
            )
            close[indices] = rows["close"].to_numpy(dtype=np.float64)
            volume[indices] = rows["volume"].to_numpy(dtype=np.float64)
        else:
            occupied = np.zeros(np.prod(shape), dtype=np.bool_)
            result = connection.execute(sql)
            to_arrow_reader = getattr(result, "to_arrow_reader", None)
            reader = (to_arrow_reader or result.fetch_record_batch)(batch_rows)
            try:
                for batch in reader:
                    indices = tuple(
                        batch.column(name).to_numpy(zero_copy_only=False)
                        for name in ("date_idx", "asset_idx", "step_idx")
                    )
                    flat_idx = (indices[0] * shape[1] + indices[1]) * shape[2] + indices[2]
                    ordered_idx = np.sort(flat_idx)
                    if (
                        np.any(ordered_idx[1:] == ordered_idx[:-1])
                        or occupied[flat_idx].any()
                    ):
                        raise ValueError("duplicate coordinates")
                    occupied[flat_idx] = True
                    close[indices] = batch.column("close").to_numpy(
                        zero_copy_only=False
                    )
                    volume[indices] = batch.column("volume").to_numpy(
                        zero_copy_only=False
                    )
            finally:
                reader.close()
    finally:
        connection.close()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    return {
        "mode": mode,
        "wall_seconds": round(perf_counter() - started, 3),
        "peak_rss_mib": round(peak / 1024 / 1024, 1),
    }


def main() -> None:
    """解析参数并分别以旧新两种模式运行性能剖析。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--batch-rows", type=int, default=500_000)
    parser.add_argument("--mode", choices=("old", "new"))
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    if args.mode:
        print(json.dumps(_profile(args.mode, args.path, args.batch_rows)))
        return
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "minute.parquet"
        _make_fixture(path, args.rows)
        for mode in ("old", "new"):
            subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--mode",
                    mode,
                    "--path",
                    str(path),
                    "--batch-rows",
                    str(args.batch_rows),
                ],
                check=True,
            )


if __name__ == "__main__":
    main()
