"""提供临时已保存因子仓库，并支持将已保存因子作为数据源引用。"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .data_provider.normalize import normalize_batches
from .data_provider.readers import RawBatch
from .domain import ValueKind, stable_hash
from .execution import ResultStream
from .formula import SourceRefExpr
from .model import (
    DataProvider,
    DataProviderError,
    InputSpec,
    ReadDomain,
    ResultAssemblyError,
    SourceBinding,
    SourceSpec,
    SourceTerm,
)


class TemporaryFactorRepository:
    """以最小分块仓库验证因子保存和加载语义。"""

    def __init__(self, root: str | Path) -> None:
        """在指定根目录初始化临时因子仓库。"""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, stream: ResultStream) -> tuple[str, ...]:
        """完整消费结果流并通过暂存目录原子提交全部因子。"""
        # 提交前校验所有因子标识和目标目录均安全且不存在。
        formula_ids = tuple(stream.plan.outputs)
        for formula_id in formula_ids:
            _safe_factor_id(formula_id)
            if self._factor_dir(formula_id).exists():
                raise FileExistsError(f"Factor {formula_id!r} already exists")
        staging = self.root / f".__tmp__.{uuid.uuid4().hex}"
        staging.mkdir()
        committed: list[str] = []
        chunks: dict[str, list[dict[str, Any]]] = {key: [] for key in formula_ids}
        try:
            # 消费流时逐块写入暂存目录，并记录全局日期切片。
            for chunk in stream:
                factor_stage = staging / chunk.formula_id
                factor_stage.mkdir(exist_ok=True)
                start, stop = chunk.output_slice.start, chunk.output_slice.stop
                filename = f"{start}_{stop}.npy"
                with (factor_stage / filename).open("wb") as file:
                    np.save(file, chunk.values)
                chunks[chunk.formula_id].append(
                    {"start": int(start), "stop": int(stop), "file": filename}
                )
            if not stream.succeeded:
                raise ResultAssemblyError("Cannot commit an incomplete ResultStream")
            # 流完整结束后为每个因子写入坐标和分块元数据。
            for formula_id in formula_ids:
                factor_stage = staging / formula_id
                factor_stage.mkdir(exist_ok=True)
                metadata = {
                    "factor_id": formula_id,
                    "asset_type": stream.domain.asset_type,
                    "frequency": stream.domain.frequency,
                    "calendar": stream.domain.calendar,
                    "dates": stream.domain.dates.tolist(),
                    "codes": stream.domain.codes.tolist(),
                    "steps": stream.domain.steps.tolist(),
                    "chunks": chunks[formula_id],
                }
                with (factor_stage / "metadata.json").open(
                    "w", encoding="utf-8"
                ) as file:
                    json.dump(metadata, file, ensure_ascii=True, sort_keys=True)
            # 所有元数据就绪后逐个将因子目录发布到最终位置。
            for formula_id in formula_ids:
                os.rename(staging / formula_id, self._factor_dir(formula_id))
                committed.append(formula_id)
            return formula_ids
        except Exception:
            # 部分发布失败时删除本次已经提交的因子目录。
            for formula_id in committed:
                shutil.rmtree(self._factor_dir(formula_id), ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def metadata(self, factor_id: str) -> dict[str, Any]:
        """读取指定已保存因子的元数据。"""
        # 先阻止路径逃逸，再将文件缺失转换为数据提供者错误。
        _safe_factor_id(factor_id)
        try:
            with (self._factor_dir(factor_id) / "metadata.json").open(
                "r", encoding="utf-8"
            ) as file:
                return json.load(file)
        except FileNotFoundError as exc:
            raise DataProviderError(
                f"Saved factor {factor_id!r} does not exist"
            ) from exc

    def load(self, factor_id: str, read_domain: ReadDomain) -> np.ndarray:
        """只读取与读取域重叠的磁盘分块并直接装配请求数组。"""
        metadata = self.metadata(factor_id)
        # 日期和资产必须在保存坐标中完整存在且保持请求顺序。
        date_positions = {date: i for i, date in enumerate(metadata["dates"])}
        code_positions = {code: i for i, code in enumerate(metadata["codes"])}
        try:
            dates = [date_positions[date] for date in read_domain.dates]
            codes = [code_positions[code] for code in read_domain.codes]
        except KeyError as exc:
            raise DataProviderError(
                f"Saved factor {factor_id!r} lacks coordinate {exc.args[0]!r}"
            ) from exc
        result = np.empty(
            (len(dates), len(codes), len(metadata["steps"])), dtype=np.float64
        )
        code_selector = _contiguous_slice(codes)
        base = self._factor_dir(factor_id)
        for chunk in metadata["chunks"]:
            start, stop = int(chunk["start"]), int(chunk["stop"])
            overlap = [
                (output_pos, stored_pos - start)
                for output_pos, stored_pos in enumerate(dates)
                if start <= stored_pos < stop
            ]
            if not overlap:
                continue
            values = self._load_chunk(base / chunk["file"])
            output_rows = [item[0] for item in overlap]
            chunk_rows = [item[1] for item in overlap]
            output_slice = _contiguous_slice(output_rows)
            chunk_slice = _contiguous_slice(chunk_rows)
            if isinstance(output_slice, slice) and isinstance(chunk_slice, slice):
                result[output_slice] = values[chunk_slice, code_selector, :]
            else:
                for output_row, chunk_row in overlap:
                    result[output_row] = values[chunk_row, code_selector, :]
            del values
        return result

    def _load_chunk(self, path: Path) -> np.ndarray:
        """读取一个仓库分块，并在返回前关闭其文件描述符。"""
        with path.open("rb") as file:
            return np.load(file)

    def _factor_dir(self, factor_id: str) -> Path:
        """返回指定因子在临时仓库中的目录。"""
        return self.root / factor_id


class RepositoryDataProvider:
    """将已保存因子与一个普通数据提供者组合为统一来源。"""

    def __init__(
        self, base: DataProvider, repository: TemporaryFactorRepository
    ) -> None:
        """组合普通数据提供者和临时因子仓库。"""
        self.base = base
        self.repository = repository

    def calendar_dates(self, calendar: str) -> np.ndarray:
        """委托基础数据提供者返回交易日期轴。"""
        return self.base.calendar_dates(calendar)

    def asset_codes(
        self,
        asset_type: str,
        dates: Sequence[Any] | None = None,
        selector: str | Sequence[Any] = "all",
    ) -> np.ndarray:
        """委托基础数据提供者返回资产代码主轴。"""
        return self.base.asset_codes(asset_type, dates, selector)

    def describe_many(
        self, source_refs: Sequence[SourceRefExpr]
    ) -> Mapping[SourceRefExpr, InputSpec]:
        """分别从临时仓库元数据和基础提供者描述输入。"""
        # factor 前缀输入由仓库描述，其余批量委托基础提供者。
        factors = [ref for ref in source_refs if ref.logical_key.startswith("factor:")]
        ordinary = [
            ref for ref in source_refs if not ref.logical_key.startswith("factor:")
        ]
        result = dict(self.base.describe_many(ordinary)) if ordinary else {}
        for ref in factors:
            metadata = self.repository.metadata(ref.logical_key.removeprefix("factor:"))
            result[ref] = InputSpec(
                metadata["asset_type"],
                metadata["frequency"],
                len(metadata["steps"]),
                ValueKind.NUMERIC,
                metadata["calendar"],
            )
        return result

    def bind_many(
        self, source_terms: Sequence[SourceTerm], read_domain: ReadDomain
    ) -> Sequence[SourceBinding]:
        """分别为已保存因子和普通数据源生成物理绑定。"""
        # 普通数据源先委托绑定，再为保存因子构造仓库专用绑定。
        factors = [
            term
            for term in source_terms
            if term.source_ref.logical_key.startswith("factor:")
        ]
        ordinary = [term for term in source_terms if term not in factors]
        result = list(self.base.bind_many(ordinary, read_domain)) if ordinary else []
        for term in factors:
            factor_id = term.source_ref.logical_key.removeprefix("factor:")
            metadata = self.repository.metadata(factor_id)
            assert term.domain is not None
            assert term.domain.codes is not None
            # 因子沿用 Term 原生资产轴和持久化 step 坐标。
            factor_domain = ReadDomain(
                read_domain.dates,
                read_domain.write_dates,
                term.domain.codes,
                tuple(metadata["steps"]),
                read_domain.output_slice,
            )
            source_spec = SourceSpec(
                metadata["asset_type"],
                metadata["frequency"],
                factor_id,
                source="temporary_factor_repository",
                params={"factor_id": factor_id},
            )
            result.append(
                SourceBinding(
                    term.term_id,
                    source_spec,
                    factor_domain,
                    stable_hash(
                        "saved_factor",
                        factor_id,
                        factor_domain.dates,
                        factor_domain.codes,
                        factor_domain.steps,
                    ),
                )
            )
        return result

    def load_many(self, bindings: Sequence[SourceBinding]) -> Mapping[str, np.ndarray]:
        """从临时仓库和基础提供者批量加载对应绑定。"""
        # 按来源拆组，普通绑定批量委托，因子绑定逐项从仓库装配。
        factors = [
            binding
            for binding in bindings
            if binding.source_spec.source == "temporary_factor_repository"
        ]
        ordinary = [binding for binding in bindings if binding not in factors]
        result = dict(self.base.load_many(ordinary)) if ordinary else {}
        # 已保存因子同样经 LoadNormalizer 授权后才进入 Runtime。
        if factors:
            dense = {
                binding.term_id: self.repository.load(
                    str(binding.source_spec.params["factor_id"]),
                    binding.read_domain,
                )
                for binding in factors
            }
            result.update(
                normalize_batches(tuple(factors), [RawBatch("dense", dense)])
            )
        return result


def _safe_factor_id(factor_id: str) -> None:
    """拒绝可能逃逸仓库根目录的不安全因子标识。"""
    if not factor_id or factor_id in {".", ".."} or Path(factor_id).name != factor_id:
        raise ValueError(f"Unsafe factor_id {factor_id!r}")


def _contiguous_slice(positions: Sequence[int]) -> slice | np.ndarray:
    """连续升序位置使用 basic slice，其余保留必要的 advanced index。"""
    start = positions[0]
    if all(position == start + offset for offset, position in enumerate(positions)):
        return slice(start, start + len(positions))
    return np.asarray(positions, dtype=np.intp)
