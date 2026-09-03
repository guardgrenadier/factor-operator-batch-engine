"""覆盖 Memory/Repository 提供者经 LoadNormalizer 进入 Runtime 的契约测试。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from factor_engine import (
    MemoryDataProvider,
    ReadDomain,
    RepositoryDataProvider,
    SourceBinding,
    SourceSpec,
    TemporaryFactorRepository,
    ValueKind,
)
from factor_engine.model import DataProviderError

DATES = ["20240102", "20240103"]
CODES = [101, 202]
DOMAIN = ReadDomain(
    ("20240102", "20240103"),
    ("20240102", "20240103"),
    (101, 202),
    (0,),
    slice(0, 2),
)


def _memory_provider(data: dict) -> MemoryDataProvider:
    """构造只含单个数据源的内存数据提供方。"""
    return MemoryDataProvider(dates=DATES, asset_codes={"stk": CODES}, data=data)


def _memory_binding(
    term_id: str, key: str, *, kind: ValueKind = ValueKind.NUMERIC
) -> SourceBinding:
    """构造绑定到内存数据源与共同读取域的测试 SourceBinding。"""
    return SourceBinding(
        term_id,
        SourceSpec.from_key(key, source="memory", field=key.split(".")[-1]),
        DOMAIN,
        "group",
        kind,
    )


def test_memory_load_normalizes_dtype_infinity_and_readonly() -> None:
    """验证内存加载统一为 float64、Infinity 转 NaN 且结果只读。"""
    data = np.arange(1, 5, dtype=np.int64).reshape(2, 2, 1)
    data = data.astype(np.float64)
    data[0, 1, 0] = np.inf
    provider = _memory_provider({"stk.1d.close": data})
    binding = _memory_binding("close", "stk.1d.close")

    result = provider.load_many([binding])

    array = result["close"]
    assert array.dtype == np.float64
    assert not array.flags.writeable
    np.testing.assert_allclose(
        array[:, :, 0], [[1.0, np.nan], [3.0, 4.0]], equal_nan=True
    )


def test_memory_load_validates_mask_and_code_value_kinds() -> None:
    """验证内存加载同样执行 MASK 0/1 与 CODE 整数校验。"""
    provider = _memory_provider(
        {
            "stk.1d.mask": np.full((2, 2, 1), 2.0),
            "stk.1d.code": np.full((2, 2, 1), 1.5),
        }
    )

    with pytest.raises(DataProviderError, match="values outside 0/1"):
        provider.load_many([_memory_binding("m", "stk.1d.mask", kind=ValueKind.MASK)])
    with pytest.raises(DataProviderError, match="non-integer values"):
        provider.load_many([_memory_binding("c", "stk.1d.code", kind=ValueKind.CODE)])


def test_memory_load_rejects_step_shortage() -> None:
    """验证内存数据 step 维小于读取域时被拒绝而不是静默错位。"""
    provider = _memory_provider({"stk.1d.close": np.ones((2, 2, 1))})
    binding = SourceBinding(
        "close",
        SourceSpec.from_key("stk.1d.close", source="memory", field="close"),
        ReadDomain(
            ("20240102", "20240103"),
            ("20240102", "20240103"),
            (101, 202),
            (0, 1),
            slice(0, 2),
        ),
        "group",
    )

    with pytest.raises(DataProviderError, match="step"):
        provider.load_many([binding])


def _write_factor(repository: TemporaryFactorRepository, values: np.ndarray) -> None:
    """以最小元数据直接向临时仓库写入一个已保存因子。"""
    factor_dir = repository.root / "factor_a"
    factor_dir.mkdir()
    with (factor_dir / "0_2.npy").open("wb") as file:
        np.save(file, values)
    metadata = {
        "factor_id": "factor_a",
        "asset_type": "stk",
        "frequency": "1d",
        "calendar": "default",
        "dates": DATES,
        "codes": CODES,
        "steps": [0],
        "chunks": [{"start": 0, "stop": 2, "file": "0_2.npy"}],
    }
    with (factor_dir / "metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file)


def _repository_binding(term_id: str, *, kind: ValueKind = ValueKind.NUMERIC):
    """构造绑定到临时仓库因子的测试 SourceBinding。"""
    return SourceBinding(
        term_id,
        SourceSpec(
            "stk",
            "1d",
            "factor_a",
            source="temporary_factor_repository",
            params={"factor_id": "factor_a"},
        ),
        DOMAIN,
        "group",
        kind,
    )


def test_repository_load_normalizes_infinity_and_readonly(tmp_path) -> None:
    """验证已保存因子加载统一处理 Infinity 并返回只读 float64 数组。"""
    repository = TemporaryFactorRepository(tmp_path / "factors")
    values = np.arange(1, 5, dtype=np.float64).reshape(2, 2, 1)
    values[1, 0, 0] = -np.inf
    _write_factor(repository, values)
    provider = RepositoryDataProvider(_memory_provider({}), repository)

    result = provider.load_many([_repository_binding("factor_a")])

    array = result["factor_a"]
    assert array.dtype == np.float64
    assert not array.flags.writeable
    np.testing.assert_allclose(
        array[:, :, 0], [[1.0, 2.0], [np.nan, 4.0]], equal_nan=True
    )


def test_repository_load_validates_declared_value_kind(tmp_path) -> None:
    """验证已保存因子加载按绑定声明的 ValueKind 校验。"""
    repository = TemporaryFactorRepository(tmp_path / "factors")
    _write_factor(repository, np.full((2, 2, 1), 2.0))
    provider = RepositoryDataProvider(_memory_provider({}), repository)

    with pytest.raises(DataProviderError, match="values outside 0/1"):
        provider.load_many([_repository_binding("factor_a", kind=ValueKind.MASK)])
