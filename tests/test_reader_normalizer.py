"""覆盖 Reader 注册表与唯一 Source Load 规范化边界。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factor_engine import (
    DataProviderError,
    DatasetSpec,
    RawBatch,
    ReadDomain,
    ReaderRequest,
    SourceBinding,
    SourceSpec,
    ValueKind,
)
from factor_engine.data_provider.datasets import (
    READER_MODES,
    READER_REGISTRY,
    SQL_QUERY_BUILDERS,
)
from factor_engine.data_provider.normalize import LoadNormalizer, normalize_source_arrays


def _domain(*, steps=(0,)) -> ReadDomain:
    return ReadDomain(
        ("20240102", "20240103"),
        ("20240102", "20240103"),
        (101, 202),
        tuple(steps),
        slice(0, 2),
    )


def _binding(
    term_id: str,
    domain: ReadDomain,
    *,
    kind: ValueKind = ValueKind.NUMERIC,
    default=np.nan,
    field: str | None = None,
    constant=None,
    params=None,
) -> SourceBinding:
    return SourceBinding(
        term_id,
        SourceSpec(
            "stk",
            "1d",
            term_id,
            field=field,
            params=dict(params or {}),
            dataset_id="dataset",
            constant=constant,
            default=default,
        ),
        domain,
        "group",
        kind,
    )


def test_reader_and_query_builder_registries_have_the_target_sets() -> None:
    assert set(READER_REGISTRY) == {
        "sql_reader",
        "fundamental",
        "parquet_bars",
        "cb_stock_map",
    }
    assert set(READER_MODES) == set(READER_REGISTRY)
    assert set(SQL_QUERY_BUILDERS) == {
        "panel_fields",
        "adjust_factor",
        "untradable",
    }


def test_labels_normalizer_applies_defaults_then_normalizes_values() -> None:
    domain = _domain()
    numeric = _binding("numeric", domain)
    mask = _binding("mask", domain, kind=ValueKind.MASK, default=0.0)

    result = LoadNormalizer((numeric, mask), "labels").normalize(
        [
            RawBatch(
                "labels",
                {"date": ["2024-01-02", "20240103"], "asset": [101, 202]},
                {"numeric": [np.inf, "4.5"], "mask": [np.inf, 1]},
            )
        ]
    )

    np.testing.assert_allclose(
        result["numeric"][:, :, 0],
        [[np.nan, np.nan], [np.nan, 4.5]],
        equal_nan=True,
    )
    np.testing.assert_allclose(
        result["mask"][:, :, 0], [[np.nan, 0.0], [0.0, 1.0]], equal_nan=True
    )
    assert result["numeric"].dtype == np.float64
    assert not result["numeric"].flags.writeable
    assert not result["mask"].flags.writeable


def test_flat_normalizer_rejects_duplicates_across_batches_and_closes_stream() -> None:
    domain = ReadDomain(
        ("20240102",),
        ("20240102",),
        (101,),
        (930, 931),
        slice(0, 1),
    )
    binding = _binding("price", domain)
    closed = False

    def batches():
        nonlocal closed
        try:
            yield RawBatch("flat", {"flat_idx": [0]}, {"price": [1.0]})
            yield RawBatch("flat", {"flat_idx": [0]}, {"price": [2.0]})
        finally:
            closed = True

    with pytest.raises(DataProviderError, match="duplicate"):
        LoadNormalizer((binding,), "flat").normalize(batches())

    assert closed


def test_static_normalizer_broadcasts_dates_without_expanding_raw_rows() -> None:
    domain = _domain()
    binding = _binding("stock_code", domain, kind=ValueKind.CODE)

    result = LoadNormalizer((binding,), "static").normalize(
        [RawBatch("static", {"asset": [202]}, {"stock_code": [33]})]
    )

    np.testing.assert_allclose(
        result["stock_code"][:, :, 0],
        [[np.nan, 33.0], [np.nan, 33.0]],
        equal_nan=True,
    )


def test_normalizer_rejects_incomplete_or_non_vector_raw_batches() -> None:
    domain = _domain()
    binding = _binding("value", domain)

    with pytest.raises(DataProviderError, match="term_ids"):
        LoadNormalizer((binding,), "labels").normalize(
            [RawBatch("labels", {"date": [], "asset": []}, {})]
        )
    with pytest.raises(DataProviderError, match="one-dimensional"):
        LoadNormalizer((binding,), "labels").normalize(
            [
                RawBatch(
                    "labels",
                    {"date": [["20240102"]], "asset": [101]},
                    {"value": [1.0]},
                )
            ]
        )


def test_dense_provider_boundary_checks_shape_and_code_values() -> None:
    domain = _domain()
    binding = _binding("code", domain, kind=ValueKind.CODE)

    with pytest.raises(DataProviderError, match="returned shape"):
        normalize_source_arrays((binding,), {"code": np.ones((2, 1, 1))})
    with pytest.raises(DataProviderError, match="non-integer"):
        normalize_source_arrays(
            (binding,), {"code": np.full((2, 2, 1), 1.5)}
        )


class _Backend:
    def __init__(self, rows: pd.DataFrame) -> None:
        self.rows = rows
        self.sql = ""

    def query(self, sql: str) -> pd.DataFrame:
        self.sql = sql
        return self.rows


def _request(
    dataset: DatasetSpec,
    bindings: tuple[SourceBinding, ...],
    backend: _Backend,
    **context,
) -> ReaderRequest:
    return ReaderRequest(
        dataset,
        bindings,
        bindings[0].read_domain,
        {
            "sql_backend": backend,
            "emit": lambda **event: None,
            **context,
        },
    )


def _dataset(
    reader: str,
    *,
    dataset_id: str = "dataset",
    query_builder: str | None = None,
    **params,
) -> DatasetSpec:
    return DatasetSpec(
        dataset_id, reader, "stk", "1d", params, query_builder
    )


def test_sql_reader_batches_index_weight_and_membership() -> None:
    domain = _domain()
    selector = {"index_inner_code": 3145}
    weight = _binding("weight", domain, field="Weight", params=selector)
    member = _binding(
        "member",
        domain,
        kind=ValueKind.MASK,
        default=0.0,
        constant=1.0,
        params=selector,
    )
    dataset = _dataset(
        "sql_reader",
        query_builder="panel_fields",
        table="SmartQuant.IndexComponentWeight_Choice",
        date_col="EndDate",
        code_col="SecuInnerCode",
        selector={
            "param": "index_inner_code",
            "column": "IndexInnerCode",
            "type": "integer",
        },
    )
    backend = _Backend(
        pd.DataFrame(
            {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [0.25]}
        )
    )
    request = _request(dataset, (weight, member), backend)
    query = SQL_QUERY_BUILDERS["panel_fields"](
        request, {"weight": "value_0", "member": "value_1"}
    )

    assert "AS DataDate" in query.sql
    assert "AS InnerCode" in query.sql
    assert "AS `value_0`" in query.sql
    assert "value_1" not in query.sql
    assert backend.sql == ""

    raw = list(
        READER_REGISTRY["sql_reader"](request)
    )
    result = LoadNormalizer((weight, member), "labels").normalize(raw)

    assert "`IndexInnerCode` = 3145" in backend.sql
    assert set(raw[0].values) == {"weight", "member"}
    np.testing.assert_allclose(
        result["weight"][:, :, 0],
        [[0.25, np.nan], [np.nan, np.nan]],
        equal_nan=True,
    )
    np.testing.assert_array_equal(result["member"][:, :, 0], [[1, 0], [0, 0]])


def test_fundamental_reader_decodes_rank_to_step() -> None:
    domain = _domain(steps=(0, 1))
    params = {"quarters": 2, "publ_date_limit": -180}
    binding = _binding("fund", domain, field="CumLatest", params=params)
    dataset = _dataset(
        "fundamental",
        table="SmartQuant.Fundamental_Item1",
        date_col="DataDate",
        code_col="InnerCode",
        rank_col="EndDateRank",
        report_date_col="EndDate",
        publication_date_col="InfoPublDate",
    )
    backend = _Backend(
        pd.DataFrame(
            {
                "DataDate": ["20240102", "20240102"],
                "InnerCode": [101, 101],
                "EndDateRank": [1, 2],
                "value_0": [11.0, 10.0],
            }
        )
    )

    raw = list(
        READER_REGISTRY["fundamental"](
            _request(dataset, (binding,), backend)
        )
    )
    result = LoadNormalizer((binding,), "labels").normalize(raw)

    assert raw[0].coordinates["step"].tolist() == [1, 0]
    np.testing.assert_allclose(
        result["fund"][0, 0], [10.0, 11.0], equal_nan=True
    )


def test_adjust_and_untradable_builders_run_through_sql_reader() -> None:
    domain = _domain()
    adjust_binding = _binding("adjust", domain)
    anchor = _dataset(
        "sql_reader",
        dataset_id="anchor",
        query_builder="panel_fields",
        table="SmartQuant.ReturnDaily",
        date_col="DataDate",
        code_col="InnerCode",
        trading_flag_col="IfTradingDay",
    )
    adjust_dataset = _dataset(
        "sql_reader",
        query_builder="adjust_factor",
        anchor_dataset_id="anchor",
        factor_table="JYDB.DZ_AdjustingFactor",
    )
    adjust_backend = _Backend(
        pd.DataFrame(
            {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [1.2]}
        )
    )

    raw = list(
        READER_REGISTRY["sql_reader"](
            _request(
                adjust_dataset,
                (adjust_binding,),
                adjust_backend,
                anchor_dataset=anchor,
            )
        )
    )
    assert raw[0].coordinate_mode == "labels"
    assert "ORDER BY a.ExDiviDate DESC LIMIT 1" in adjust_backend.sql

    mask_binding = _binding(
        "mask", domain, kind=ValueKind.MASK, default=0.0
    )
    untradable_dataset = _dataset(
        "sql_reader",
        query_builder="untradable",
        table="SmartQuant.Untradable",
        date_col="DataDate",
        code_col="InnerCode",
    )
    untradable_backend = _Backend(
        pd.DataFrame(
            {"DataDate": ["20240102"], "InnerCode": [101], "value_0": [1]}
        )
    )

    raw = list(
        READER_REGISTRY["sql_reader"](
            _request(untradable_dataset, (mask_binding,), untradable_backend)
        )
    )
    assert raw[0].coordinate_mode == "labels"
    assert "COALESCE(`IfSuspended`, 0) = 1" in untradable_backend.sql
    assert "COALESCE(`IfLimitup`, 0) = 1" in untradable_backend.sql
