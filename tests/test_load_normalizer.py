"""覆盖 LoadNormalizer 作为唯一 Source 规范化边界的数组协议契约测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from factor_engine import ReadDomain, SourceBinding, SourceSpec, ValueKind
from factor_engine.data_provider.normalize import normalize_batches
from factor_engine.data_provider.readers import RawBatch
from factor_engine.model import DataProviderError


DOMAIN = ReadDomain(
    ("20240102", "20240103"),
    ("20240102", "20240103"),
    (101, 202),
    (0,),
    slice(0, 2),
)


def _binding(
    term_id: str,
    *,
    kind: ValueKind = ValueKind.NUMERIC,
    params: dict | None = None,
) -> SourceBinding:
    """构造一个绑定到共同读取域的测试 SourceBinding。"""
    return SourceBinding(
        term_id,
        SourceSpec("stk", "1d", term_id, params=dict(params or {})),
        DOMAIN,
        "group",
        kind,
    )


def _labels(rows: dict) -> RawBatch:
    """构造 labels 坐标模式的 RawBatch。"""
    return RawBatch("labels", pd.DataFrame(rows))


def test_labels_scatter_with_constant_column_and_explicit_default() -> None:
    """验证常量列投影在既有行上生效，缺失位置由显式默认值填充。"""
    weight = _binding("weight")
    member = _binding(
        "member", kind=ValueKind.MASK, params={"constant": 1, "default": 0.0}
    )
    batch = _labels(
        {
            "DataDate": ["20240102"],
            "InnerCode": [101],
            "value_0": [0.25],
            "value_1": [1.0],
        }
    )

    result = normalize_batches((weight, member), [batch])

    np.testing.assert_allclose(
        result["weight"][:, :, 0],
        [[0.25, np.nan], [np.nan, np.nan]],
        equal_nan=True,
    )
    np.testing.assert_array_equal(
        result["member"][:, :, 0], [[1.0, 0.0], [0.0, 0.0]]
    )


def test_empty_labels_batch_yields_defaults() -> None:
    """验证空批次返回全默认值数组且 term_id 集合完整。"""
    binding = _binding("member", kind=ValueKind.MASK, params={"default": 0.0})
    batch = _labels({"DataDate": [], "InnerCode": [], "value_0": []})

    result = normalize_batches((binding,), [batch])

    assert set(result) == {"member"}
    np.testing.assert_array_equal(result["member"], np.zeros((2, 2, 1)))


def test_normalized_arrays_are_read_only_and_float64() -> None:
    """验证规范化结果只读且 dtype 为 float64。"""
    binding = _binding("x")
    batch = _labels(
        {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [1]}
    )

    result = normalize_batches((binding,), [batch])

    assert result["x"].dtype == np.float64
    assert not result["x"].flags.writeable


def test_labels_reject_duplicate_and_out_of_domain_coordinates() -> None:
    """验证拒绝重复坐标与读取域外坐标。"""
    binding = _binding("x")
    duplicate = _labels(
        {
            "DataDate": ["20240102", "20240102"],
            "InnerCode": [101, 101],
            "value_0": [1.0, 2.0],
        }
    )
    outside = _labels(
        {"DataDate": ["20240102"], "InnerCode": [999], "value_0": [1.0]}
    )

    with pytest.raises(DataProviderError, match="duplicate date/asset/step"):
        normalize_batches((binding,), [duplicate])
    with pytest.raises(DataProviderError, match="outside ReadDomain"):
        normalize_batches((binding,), [outside])


def test_labels_with_step_column_scatter_to_step_axis() -> None:
    """验证带 Step 规范列的 labels 批次散布到 step 轴。"""
    domain = ReadDomain(
        ("20240102",), ("20240102",), (101,), (0, 1), slice(0, 1)
    )
    binding = SourceBinding(
        "fund",
        SourceSpec("stk", "1d", "fund", params={"quarters": 2}),
        domain,
        "group",
    )
    batch = _labels(
        {
            "DataDate": ["20240102", "20240102"],
            "InnerCode": [101, 101],
            "Step": [0, 1],
            "value_0": [10.0, 20.0],
        }
    )

    result = normalize_batches((binding,), [batch])

    np.testing.assert_array_equal(result["fund"][0, 0], [10.0, 20.0])


def test_mask_and_code_value_kinds_are_validated() -> None:
    """验证 MASK 只接受 0/1/NaN，CODE 只接受整数/NaN。"""
    mask = _binding("mask", kind=ValueKind.MASK)
    code = _binding("code", kind=ValueKind.CODE)
    bad_mask = _labels(
        {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [2.0]}
    )
    bad_code = _labels(
        {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [1.5]}
    )

    with pytest.raises(DataProviderError, match="values outside 0/1"):
        normalize_batches((mask,), [bad_mask])
    with pytest.raises(DataProviderError, match="non-integer values"):
        normalize_batches((code,), [bad_code])


def test_null_and_infinity_become_nan() -> None:
    """验证 NULL、缺失记录与正负 Infinity 统一为 NaN。"""
    binding = _binding("x")
    batch = _labels(
        {
            "DataDate": ["20240102", "20240102", "20240103"],
            "InnerCode": [101, 202, 101],
            "value_0": [np.inf, -np.inf, 1.0],
        }
    )

    result = normalize_batches((binding,), [batch])

    np.testing.assert_allclose(
        result["x"][:, :, 0],
        [[np.nan, np.nan], [1.0, np.nan]],
        equal_nan=True,
    )


def test_static_batch_broadcasts_along_dates_and_skips_unknown_codes() -> None:
    """验证 static 批次沿日期轴广播，并跳过任务资产轴外的代码。"""
    binding = _binding("mapping", kind=ValueKind.CODE)
    batch = RawBatch(
        "static",
        pd.DataFrame({"InnerCode": [101, 999], "value_0": [7.0, 8.0]}),
    )

    result = normalize_batches((binding,), [batch])

    np.testing.assert_allclose(
        result["mapping"][:, :, 0],
        [[7.0, np.nan], [7.0, np.nan]],
        equal_nan=True,
    )


def test_flat_batches_scatter_streaming_positions() -> None:
    """验证 flat 批次按 flat_idx 位置跨批次散布并拒绝重复位置。"""
    binding = _binding("x")
    first = RawBatch(
        "flat",
        pa.record_batch(
            {
                "flat_idx": pa.array([0, 3], type=pa.int64()),
                "value_0": pa.array([1.0, 4.0], type=pa.float64()),
            }
        ),
    )
    second = RawBatch(
        "flat",
        pa.record_batch(
            {
                "flat_idx": pa.array([1], type=pa.int64()),
                "value_0": pa.array([np.inf], type=pa.float64()),
            }
        ),
    )

    result = normalize_batches((binding,), [first, second])

    np.testing.assert_allclose(
        result["x"][:, :, 0],
        [[1.0, np.nan], [np.nan, 4.0]],
        equal_nan=True,
    )
    duplicate = RawBatch(
        "flat",
        pa.record_batch(
            {
                "flat_idx": pa.array([0], type=pa.int64()),
                "value_0": pa.array([9.0], type=pa.float64()),
            }
        ),
    )
    with pytest.raises(DataProviderError, match="duplicate date/asset/step"):
        normalize_batches((binding,), [first, duplicate])


def test_unknown_batch_mode_is_rejected() -> None:
    """验证未知坐标模式被拒绝。"""
    with pytest.raises(DataProviderError, match="Unknown RawBatch coordinate mode"):
        normalize_batches((_binding("x"),), [RawBatch("mystery", pd.DataFrame())])


def test_labels_reject_cross_batch_duplicate_coordinates() -> None:
    """验证 labels 模式在整个批次流范围内拒绝重复坐标。"""
    binding = _binding("x")
    first = _labels(
        {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [1.0]}
    )
    second = _labels(
        {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [2.0]}
    )

    with pytest.raises(DataProviderError, match="duplicate date/asset/step"):
        normalize_batches((binding,), [first, second])


def test_labels_reject_normalized_duplicate_coordinates() -> None:
    """验证不同原始表示规范化到同一位置后被拒绝。"""
    binding = _binding("x")
    # "2024-01-02" 与 "20240102" 规范化到同一日期；"101" 与 101 到同一资产。
    batch = _labels(
        {
            "DataDate": ["2024-01-02", "20240102"],
            "InnerCode": ["101", 101],
            "value_0": [1.0, 2.0],
        }
    )
    cross_batch = _labels(
        {"DataDate": ["2024-01-02"], "InnerCode": [101], "value_0": [3.0]}
    )

    with pytest.raises(DataProviderError, match="duplicate date/asset/step"):
        normalize_batches((binding,), [batch])
    with pytest.raises(DataProviderError, match="duplicate date/asset/step"):
        normalize_batches(
            (binding,),
            [
                _labels(
                    {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [1.0]}
                ),
                cross_batch,
            ],
        )


def test_labels_reject_non_integer_coordinates() -> None:
    """验证拒绝会被静默截断的非整数资产与 step 坐标。"""
    binding = _binding("x")
    bad_code = _labels(
        {"DataDate": ["20240102"], "InnerCode": [101.5], "value_0": [1.0]}
    )
    bad_step = _labels(
        {
            "DataDate": ["20240102"],
            "InnerCode": [101],
            "Step": [0.5],
            "value_0": [1.0],
        }
    )

    with pytest.raises(DataProviderError, match="non-integer asset coordinates"):
        normalize_batches((binding,), [bad_code])
    with pytest.raises(DataProviderError, match="non-integer step coordinates"):
        normalize_batches((binding,), [bad_step])


def test_static_reject_cross_batch_and_normalized_duplicates() -> None:
    """验证 static 模式跨批次以及规范化后的重复资产坐标被拒绝。"""
    binding = _binding("mapping", kind=ValueKind.CODE)
    first = RawBatch("static", pd.DataFrame({"InnerCode": [101], "value_0": [7.0]}))
    second = RawBatch("static", pd.DataFrame({"InnerCode": [101], "value_0": [8.0]}))
    normalized = RawBatch(
        "static", pd.DataFrame({"InnerCode": ["101", 101], "value_0": [7.0, 8.0]})
    )
    # 资产轴外的重复行不写入任何位置，允许跳过。
    out_of_domain = RawBatch(
        "static", pd.DataFrame({"InnerCode": [999, 999.0], "value_0": [1.0, 2.0]})
    )

    with pytest.raises(DataProviderError, match="duplicate static asset"):
        normalize_batches((binding,), [first, second])
    with pytest.raises(DataProviderError, match="duplicate static asset"):
        normalize_batches((binding,), [normalized])

    result = normalize_batches((binding,), [out_of_domain])
    np.testing.assert_array_equal(
        result["mapping"], np.full((2, 2, 1), np.nan)
    )


def test_dense_batches_authorize_aligned_arrays() -> None:
    """验证 dense 批次接受坐标已对齐数组并统一值协议与只读契约。"""
    binding = _binding("x")
    array = np.arange(4, dtype=np.int64).reshape(2, 2, 1).astype(np.int64)
    array = array + 0.5  # 非 float64 输入也统一转换
    array[0, 1, 0] = np.inf

    result = normalize_batches((binding,), [RawBatch("dense", {"x": array})])

    assert result["x"].dtype == np.float64
    assert not result["x"].flags.writeable
    np.testing.assert_allclose(
        result["x"][:, :, 0],
        [[0.5, np.nan], [2.5, 3.5]],
        equal_nan=True,
    )


def test_dense_rejects_shape_mismatch_unknown_term_and_redelivery() -> None:
    """验证 dense 批次拒绝形状不符、未知 term 与重复交付。"""
    binding = _binding("x")

    with pytest.raises(DataProviderError, match="has shape"):
        normalize_batches(
            (binding,), [RawBatch("dense", {"x": np.zeros((2, 2, 2))})]
        )
    with pytest.raises(DataProviderError, match="unknown term"):
        normalize_batches(
            (binding,), [RawBatch("dense", {"y": np.zeros((2, 2, 1))})]
        )
    with pytest.raises(DataProviderError, match="more than once"):
        normalize_batches(
            (binding,),
            [
                RawBatch("dense", {"x": np.zeros((2, 2, 1))}),
                RawBatch("dense", {"x": np.ones((2, 2, 1))}),
            ],
        )


def test_dense_validates_mask_and_code_value_kinds() -> None:
    """验证 dense 批次同样执行 MASK 0/1 与 CODE 整数校验。"""
    mask = _binding("mask", kind=ValueKind.MASK)
    code = _binding("code", kind=ValueKind.CODE)
    bad_mask = np.full((2, 2, 1), 2.0)
    bad_code = np.full((2, 2, 1), 1.5)

    with pytest.raises(DataProviderError, match="values outside 0/1"):
        normalize_batches((mask,), [RawBatch("dense", {"mask": bad_mask})])
    with pytest.raises(DataProviderError, match="non-integer values"):
        normalize_batches((code,), [RawBatch("dense", {"code": bad_code})])


def test_explicit_default_follows_value_contract() -> None:
    """验证显式默认值与物理数据共用 Infinity 与 ValueKind 校验。"""
    mask = _binding("mask", kind=ValueKind.MASK, params={"default": 2.0})
    code = _binding("code", kind=ValueKind.CODE, params={"default": 1.5})
    infinite = _binding("inf", params={"default": np.inf})
    negative_infinite = _binding("neg_inf", params={"default": -np.inf})
    valid = _binding("member", kind=ValueKind.MASK, params={"default": 1.0})
    non_numeric = _binding("text", params={"default": "abc"})

    with pytest.raises(DataProviderError, match="values outside 0/1"):
        normalize_batches((mask,), [])
    with pytest.raises(DataProviderError, match="non-integer values"):
        normalize_batches((code,), [])
    with pytest.raises(DataProviderError, match="non-numeric values"):
        normalize_batches((non_numeric,), [])

    result = normalize_batches((infinite, negative_infinite, valid), [])
    np.testing.assert_array_equal(result["inf"], np.full((2, 2, 1), np.nan))
    np.testing.assert_array_equal(result["neg_inf"], np.full((2, 2, 1), np.nan))
    np.testing.assert_array_equal(result["member"], np.ones((2, 2, 1)))
