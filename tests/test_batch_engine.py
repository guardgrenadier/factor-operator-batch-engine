"""覆盖批量因子引擎编译、执行、流式结果与因子仓储的端到端测试。"""

from __future__ import annotations

import gc
from dataclasses import replace
import weakref

import numpy as np
import pandas as pd
import pytest

from factor_engine import (
    BatchFactorEngine,
    CompileError,
    ComputeRequest,
    DataProviderError,
    DomainError,
    DomainSpec,
    ExecutionOptions,
    InputSpec,
    MemoryDataProvider,
    OperatorTerm,
    ReadDomain,
    RepositoryDataProvider,
    SourceTerm,
    TemporaryFactorRepository,
    ValueKind,
)
from factor_engine import execution as execution_module
from factor_engine.formula import FormulaBatch
from factor_engine.operators import OperatorSpec, default_operator_registry


DATES = ["20240102", "20240103", "20240104", "20240105", "20240108"]
CODES = [101, 202]


def _provider(**kwargs) -> MemoryDataProvider:
    """构造含日频收盘价与成交量数据的内存数据提供方。"""
    close = np.arange(1, 11, dtype=np.float64).reshape(5, 2)
    volume = np.full((5, 2), 10.0, dtype=np.float64)
    data = {"stk.1d.close": close, "stk.1d.volume": volume, **kwargs.pop("data", {})}
    return MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data=data,
        **kwargs,
    )


def _request(
    formulas: dict[str, str],
    *,
    common_inputs: str = "close = source('stk.1d.close')\nvolume = source('stk.1d.volume')",
    start: str = "20240102",
    end: str = "20240108",
    assets="all",
) -> ComputeRequest:
    """构造引用收盘价与成交量公共输入的日频计算请求。"""
    return ComputeRequest(
        DomainSpec(start, end, {"stk": assets}, "stk", "1d", 1),
        FormulaBatch.from_text(common_inputs=common_inputs, formulas=formulas),
    )


def test_compiler_builds_one_shared_term_dag_across_formula_groups() -> None:
    """验证编译器跨公式构建唯一的共享 Term 逻辑计划。"""
    engine = BatchFactorEngine(_provider())
    request = _request(
        {
            "one": "shared_one = close + volume\nfactor = shared_one * 2",
            "two": "shared_two = close + volume\nfactor = shared_two / 2",
        }
    )

    job = engine.compile(request)

    adds = [
        term
        for term in job.plan.terms.values()
        if isinstance(term, OperatorTerm) and term.operator_name == "add"
    ]
    assert len(adds) == 1
    assert (
        len([term for term in job.plan.terms.values() if isinstance(term, SourceTerm)])
        == 2
    )


def test_common_intermediate_is_reused_by_multiple_formula_outputs() -> None:
    """验证公共输入的中间结果被多个公式输出复用。"""
    request = _request(
        {
            "std_2": "factor = ts_std(indicator, 2)",
            "std_3": "factor = ts_std(indicator, 3)",
        },
        common_inputs=(
            "close = source('stk.1d.close')\n"
            "indicator = ts_mean(close, 2)"
        ),
    )

    job = BatchFactorEngine(_provider()).compile(request)

    indicators = [
        term
        for term in job.plan.terms.values()
        if isinstance(term, OperatorTerm)
        and term.operator_name == "ts_mean"
        and term.params["window"] == 2
    ]
    assert len(indicators) == 1
    assert job.plan.reference_counts[indicators[0].term_id] == 2


def test_physical_source_group_does_not_change_logical_plan_identity() -> None:
    """验证物理源分组不改变逻辑计划的语义标识。"""
    request = _request({"alpha": "factor = close + volume"})

    separate = BatchFactorEngine(_provider()).compile(request)
    grouped = BatchFactorEngine(
        _provider(load_groups={"stk.1d.close": "daily", "stk.1d.volume": "daily"})
    ).compile(request)

    assert separate.plan.semantic_id == grouped.plan.semantic_id


def test_domain_all_uses_master_axis_and_explicit_subset_preserves_order() -> None:
    """验证全量资产用主轴，显式子集保留调用方顺序。"""
    engine = BatchFactorEngine(_provider())

    all_domain = engine.compile(_request({"alpha": "factor = close"})).domain
    subset = engine.compile(
        _request({"alpha": "factor = close"}, assets=[202, 101])
    ).domain

    np.testing.assert_array_equal(all_domain.codes, [101, 202])
    np.testing.assert_array_equal(subset.codes, [202, 101])


def test_lookback_is_composed_through_term_dag() -> None:
    """验证历史回看（lookback）沿 Term 依赖图正确组合。"""
    engine = BatchFactorEngine(_provider())
    request = _request({"alpha": "mean = ts_mean(close, 2)\nfactor = ts_mean(mean, 3)"})

    job = engine.compile(request)

    assert job.plan.job_lookback == 3


def test_runtime_computes_outputs_releases_workspace_and_groups_loads() -> None:
    """验证运行时计算输出、释放工作区并合并同组加载。"""
    provider = _provider(
        load_groups={"stk.1d.close": "daily", "stk.1d.volume": "daily"}
    )
    engine = BatchFactorEngine(provider)
    request = _request(
        {
            "sum": "factor = close + volume",
            "scaled": "intermediate = close + volume\nfactor = intermediate * 2",
        }
    )

    result = engine.compute(request)

    expected = np.arange(1, 11, dtype=np.float64).reshape(5, 2, 1) + 10
    np.testing.assert_allclose(result.arrays["sum"], expected)
    np.testing.assert_allclose(result.arrays["scaled"], expected * 2)
    assert result.stats.load_calls == 1
    assert len(provider.load_calls) == 1
    assert result.stats.released_terms


def test_compute_result_dataframe_has_one_unambiguous_layout() -> None:
    """验证计算结果转 DataFrame 的布局唯一且无歧义。"""
    engine = BatchFactorEngine(_provider())
    result = engine.compute(_request({"alpha": "factor = close"}))

    frame = result.to_dataframe()

    assert frame.index.names == ["date", "asset", "step"]
    assert frame.columns.tolist() == ["alpha"]
    assert frame.shape == (10, 1)
    assert frame.loc[("20240102", 101, 0), "alpha"] == 1.0


def test_result_stream_is_ordered_single_use_and_marks_natural_completion() -> None:
    """验证结果流有序、单次消费并标记自然完成。"""
    engine = BatchFactorEngine(_provider())
    stream = engine.stream(
        _request({"one": "factor = close", "two": "factor = volume"}),
        options=ExecutionOptions(chunk_size=2),
    )

    chunks = list(stream)

    assert [(chunk.output_slice.start, chunk.formula_id) for chunk in chunks] == [
        (0, "one"),
        (0, "two"),
        (2, "one"),
        (2, "two"),
        (4, "one"),
        (4, "two"),
    ]
    assert stream.succeeded
    with pytest.raises(RuntimeError, match="only be consumed once"):
        iter(stream)


def test_chunked_and_whole_domain_results_are_identical_with_lookback() -> None:
    """验证含历史回看时分块与整域计算结果一致。"""
    request = _request({"alpha": "factor = ts_mean(close, 3)"})
    whole_provider = _provider()
    chunked_provider = _provider()

    whole = BatchFactorEngine(whole_provider).compute(request)
    chunked = BatchFactorEngine(chunked_provider).compute(
        request, options=ExecutionOptions(chunk_size=2)
    )

    np.testing.assert_allclose(
        whole.arrays["alpha"], chunked.arrays["alpha"], equal_nan=True
    )
    assert chunked_provider.bound_domains[1].dates == (
        "20240102",
        "20240103",
        "20240104",
        "20240105",
    )
    assert chunked_provider.bound_domains[1].write_dates == ("20240104", "20240105")


def test_compute_allocates_one_full_result_array_per_formula(monkeypatch) -> None:
    """验证每个公式只分配一块完整结果数组而非按块分配。"""
    allocations = 0
    original_empty = execution_module.np.empty

    def tracked_empty(shape, *args, **kwargs):
        """跟踪目标形状结果数组的分配次数。"""
        nonlocal allocations
        if tuple(shape) == (5, 2, 1):
            allocations += 1
        return original_empty(shape, *args, **kwargs)

    monkeypatch.setattr(execution_module.np, "empty", tracked_empty)

    result = BatchFactorEngine(_provider()).compute(
        _request({"alpha": "factor = close"}),
        options=ExecutionOptions(chunk_size=1),
    )

    assert allocations == 1
    np.testing.assert_array_equal(
        result.arrays["alpha"],
        np.arange(1, 11, dtype=np.float64).reshape(5, 2, 1),
    )


def test_output_uses_a_shared_memory_suffix_slice_with_lookback() -> None:
    """验证输出分块复用算子工作区内存的尾段共享切片。"""
    operators = default_operator_registry()
    original = operators["ts_mean"]
    operator_results = []

    def tracked_ts_mean(x, window=5, min_periods=None, axis=0):
        """记录 ts_mean 每次返回的数组以便验证内存共享。"""
        value = original.func(x, window=window, min_periods=min_periods, axis=axis)
        operator_results.append(value)
        return value

    operators["ts_mean"] = replace(original, func=tracked_ts_mean)
    stream = BatchFactorEngine(_provider(), operators=operators).stream(
        _request({"alpha": "factor = ts_mean(close, 3)"}),
        options=ExecutionOptions(chunk_size=2),
    )
    iterator = iter(stream)

    first = next(iterator)
    second = next(iterator)

    assert first.output_slice == slice(0, 2)
    assert second.output_slice == slice(2, 4)
    assert np.shares_memory(first.values, operator_results[0])
    assert np.shares_memory(second.values, operator_results[1])
    np.testing.assert_allclose(
        second.values[:, :, 0],
        np.array([[3.0, 4.0], [5.0, 6.0]]),
    )
    stream.close()


def test_runtime_requires_a_normalized_source_batch() -> None:
    """验证 Runtime 拒绝绕过 Source Load 规范化边界的普通映射。"""

    class BadProvider(MemoryDataProvider):
        """把可信批次降级为普通映射的坏提供方。"""

        def load_many(self, bindings):
            """丢弃规范批次标记以制造边界错误。"""
            values = super().load_many(bindings)
            return dict(values)

    provider = BadProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={"stk.1d.close": np.ones((5, 2))},
        input_specs={"stk.1d.close": InputSpec("stk", "1d", 1)},
    )
    request = _request(
        {"alpha": "factor = close"},
        common_inputs="close = source('stk.1d.close')",
    )

    with pytest.raises(DataProviderError, match="NormalizedSourceBatch"):
        BatchFactorEngine(provider).compute(request)


def test_memory_provider_normalizes_source_dtype_before_runtime() -> None:
    """验证内存 Provider 在自己的 Load 边界规范化 dtype。"""

    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={"stk.1d.close": np.ones((5, 2), dtype=np.int32)},
    )
    request = _request(
        {"alpha": "factor = close"},
        common_inputs="close = source('stk.1d.close')",
    )

    result = BatchFactorEngine(provider).compute(request)

    assert result.arrays["alpha"].dtype == np.float64


def test_compile_error_includes_formula_id_and_source_position() -> None:
    """验证编译错误包含公式标识与源码位置。"""
    request = _request({"alpha": "factor = unknown_operator(close)"})

    with pytest.raises(Exception) as error:
        BatchFactorEngine(_provider()).compile(request)

    assert "alpha:1:" in str(error.value)
    assert "formula 'alpha'" in str(error.value)


def test_dataframe_values_are_column_major_by_formula_not_interleaved() -> None:
    """验证 DataFrame 各公式列按列主序而非交错存储。"""
    engine = BatchFactorEngine(_provider())
    result = engine.compute(
        _request({"close": "factor = close", "volume": "factor = volume"})
    )

    frame = result.to_dataframe()

    pd.testing.assert_series_equal(
        frame["close"],
        pd.Series(result.arrays["close"].reshape(-1), index=frame.index, name="close"),
    )


def test_runtime_drops_load_and_operator_locals_before_workspace_release(
    monkeypatch,
) -> None:
    """验证运行时在释放工作区前先丢弃加载与算子局部引用。"""

    # 准备一个记录源数组弱引用的提供方，以及带中间绑定的公式。
    class EphemeralProvider(MemoryDataProvider):
        """记录源加载数组弱引用以检测其回收时机的提供方。"""

        source_ref = None

        def load_many(self, bindings):
            """保存加载值的弱引用后原样返回结果。"""
            loaded = super().load_many(bindings)
            self.source_ref = weakref.ref(next(iter(loaded.values())))
            return loaded

    provider = EphemeralProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={"stk.1d.close": np.ones((2, 2), dtype=np.float64)},
    )
    lifecycle = {}

    def track(source):
        """记录中间结果弱引用以便观察其释放时机。"""
        value = source + 1
        lifecycle["intermediate"] = weakref.ref(value)
        return value

    def copy_value(value):
        """返回加一结果作为中间算子。"""
        return value + 1

    def finish(value):
        """返回加一结果作为最终算子。"""
        return value + 1

    numeric = ValueKind.NUMERIC
    operators = default_operator_registry()
    operators.update(
        {
            "track": OperatorSpec("track", track, (numeric,), numeric),
            "copy_value": OperatorSpec(
                "copy_value", copy_value, (numeric,), numeric
            ),
            "finish": OperatorSpec("finish", finish, (numeric,), numeric),
        }
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={
                "alpha": (
                    "intermediate = track(close)\n"
                    "copied = copy_value(intermediate)\n"
                    "factor = finish(copied)"
                )
            },
        ),
    )
    stream = BatchFactorEngine(provider, operators=operators).stream(request)
    source_id = stream.plan.source_terms[0].term_id
    intermediate_id = next(
        term.term_id
        for term in stream.plan.terms.values()
        if isinstance(term, OperatorTerm) and term.operator_name == "track"
    )
    observed = []

    # 用观察者断言：每个 Term 释放时其对应数组已被回收。
    class ReleaseObserver(list):
        """在 Term 释放时校验数组弱引用已失效的观察器。"""

        def append(self, term_id):
            """记录释放顺序并断言源与中间数组均已回收。"""
            if term_id == source_id:
                assert provider.source_ref is not None
                assert provider.source_ref() is None
                observed.append("source")
            elif term_id == intermediate_id:
                assert lifecycle["intermediate"]() is None
                observed.append("intermediate")
            super().append(term_id)

    stream.stats.released_terms = ReleaseObserver()

    def forbidden_collect():
        """替换 gc.collect 以确保运行时不依赖主动回收。"""
        raise AssertionError("Runtime must not call gc.collect()")

    monkeypatch.setattr(gc, "collect", forbidden_collect)
    chunks = list(stream)

    assert observed == ["source", "intermediate"]
    np.testing.assert_array_equal(chunks[0].values, 4.0)


def test_daily_source_is_lowered_to_intraday_singleton_broadcast() -> None:
    """验证日频源被降低为日内单 step 广播视图而非实体算子。"""
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={"stk.1d.close": np.arange(1, 11, dtype=np.float64).reshape(5, 2)},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240108", {"stk": "all"}, "stk", "5min", 48),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={"alpha": "factor = close"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    assert result.arrays["alpha"].shape == (5, 2, 48)
    np.testing.assert_array_equal(
        result.arrays["alpha"][:, :, 0],
        np.arange(1, 11, dtype=np.float64).reshape(5, 2),
    )
    assert not any(
        isinstance(term, OperatorTerm)
        and term.operator_name in {"__broadcast_steps", "__ffill_steps"}
        for term in result.plan.terms.values()
    )


def test_singleton_step_broadcast_itself_is_a_numpy_view() -> None:
    """验证单 step 广播本身是不可写的 NumPy 视图。"""
    source = np.ones((2, 3, 1), dtype=np.float64)
    broadcast = np.broadcast_to(source, (2, 3, 48))

    assert np.shares_memory(source, broadcast)
    assert not broadcast.flags.writeable


def test_explicit_intraday_to_daily_resample() -> None:
    """验证显式日内到日频 resample 的数值与输出域对齐。"""
    intraday = np.arange(2 * 2 * 237, dtype=np.float64).reshape(2, 2, 237)
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={"stk.1min.price": intraday},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="price = source('stk.1min.price')",
            formulas={"alpha": "factor = resample(price, '1d', method='mean')"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_allclose(result.arrays["alpha"][:, :, 0], intraday.mean(axis=2))
    source_term = next(
        term for term in result.plan.terms.values() if isinstance(term, SourceTerm)
    )
    resample_term = next(
        term
        for term in result.plan.terms.values()
        if isinstance(term, OperatorTerm) and term.operator_name == "resample"
    )
    assert source_term.source_domain.frequency == "1min"
    assert source_term.layout.step_count == 237
    assert resample_term.layout.asset_count == source_term.layout.asset_count
    assert resample_term.layout.step_count == 1
    assert not hasattr(resample_term, "domain")
    assert resample_term.params == {"boundaries": ((0, 237),), "method": 0}


def test_get_hf_resample_sugar_reuses_the_public_operator_and_loads_raw_source() -> (
    None
):
    """验证 get_hf 的 resample 语法糖复用公开算子并加载原始源。"""
    intraday = np.arange(2 * 2 * 237, dtype=np.float64).reshape(2, 2, 237)
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={"stk.1min.ClosePrice": intraday},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "raw = get_hf('stk', '1min', 'ClosePrice')\n"
                "sugar = get_hf('stk', '1min', 'ClosePrice', "
                "resample='1d', method='last')"
            ),
            formulas={
                "sugar": "factor = sugar",
                "direct": "factor = resample(raw, '1d', method='last')",
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    assert result.plan.outputs["sugar"] == result.plan.outputs["direct"]
    np.testing.assert_array_equal(result.arrays["sugar"], intraday[:, :, -1:])
    source_term = result.plan.source_terms[0]
    assert source_term.source_ref.logical_key == "stk.1min.ClosePrice"
    assert source_term.source_ref.semantic_params == ()
    assert provider.load_calls == [(source_term.term_id,)]


def test_resample_does_not_adopt_a_different_target_asset_domain() -> None:
    """验证 resample 不采纳与源不符的目标资产域，提前报错。"""
    intraday = np.arange(2 * 3 * 237, dtype=np.float64).reshape(2, 3, 237)
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": [11, 22, 33], "cb": [101, 102]},
        data={"stk.1min.price": intraday},
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": "all", "cb": "all"},
            "cb",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="price = source('stk.1min.price')",
            formulas={"alpha": "factor = resample(price, '1d', method='mean')"},
        ),
    )

    with pytest.raises(DomainError, match="asset count 3 cannot broadcast"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


def test_unique_coarse_to_fine_intraday_projection_is_lowered_explicitly() -> None:
    """验证唯一粗频到细频的日内投影被显式降低为对齐算子。"""
    coarse = np.arange(2 * 2 * 4, dtype=np.float64).reshape(2, 2, 4)
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={"stk.60min.price": coarse},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "30min", 8),
        FormulaBatch.from_text(
            common_inputs="price = source('stk.60min.price')",
            formulas={
                "alpha": (
                    "factor = align_frequency(price, '30min', method='ffill')"
                )
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    assert "align_frequency" in default_operator_registry()
    np.testing.assert_array_equal(
        result.arrays["alpha"], coarse[:, :, [0, 0, 1, 1, 2, 2, 3, 3]]
    )
    assert any(
        isinstance(term, OperatorTerm) and term.operator_name == "align_frequency"
        for term in result.plan.terms.values()
    )


def test_stk_to_cb_helper_registers_and_uses_the_mapping_source() -> None:
    """验证股票到可转债映射 helper 注册并使用映射源。"""
    stock = np.array([[[10.0], [20.0]], [[11.0], [21.0]]])
    mapping = np.array([[[1.0], [0.0]], [[1.0], [0.0]]])
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": [11, 22], "cb": [101, 102]},
        data={
            "stk.1d.close": stock,
            "cb.1d.underlying_stk_col": mapping,
        },
        input_specs={
            "cb.1d.underlying_stk_col": InputSpec(
                "cb", "1d", 1, value_kind=ValueKind.CODE
            )
        },
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": "all", "cb": "all"},
            "cb",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="stock = source('stk.1d.close')",
            formulas={"alpha": "factor = project_stk_to_cb(stock)"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(result.arrays["alpha"], stock[:, [1, 0], :])
    assert any(
        isinstance(term, SourceTerm)
        and term.source_ref.logical_key == "cb.1d.underlying_stk_col"
        for term in result.plan.terms.values()
    )
    assert any(
        isinstance(term, OperatorTerm) and term.operator_name == "lookup_by_col"
        for term in result.plan.terms.values()
    )


def test_selected_index_feature_can_broadcast_to_target_asset_axis() -> None:
    """验证选定指数的值能广播到目标资产轴。"""
    index_values = np.array([[3.0, 5.0], [4.0, 6.0]])
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES, "idx": [300, 500]},
        data={"idx.1d.close": index_values},
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": "all", "idx": [300, 500]},
            "stk",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="index_close = source('idx.1d.close')",
            formulas={
                "alpha": "factor = select_index_feature(index_close, 500)"
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_allclose(
        result.arrays["alpha"][:, :, 0], np.array([[5.0, 5.0], [6.0, 6.0]])
    )
    assert {
        term.source_ref.logical_key for term in result.plan.source_terms
    } == {"idx.1d.close"}


def test_daily_index_selection_broadcasts_across_intraday_steps_and_assets() -> None:
    """验证日频指数选取跨日内 step 与资产双重广播。"""
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES, "idx": [300]},
        data={"idx.1d.close": np.array([[3.0], [4.0]])},
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": "all", "idx": "all"},
            "stk",
            "5min",
            48,
        ),
        FormulaBatch.from_text(
            common_inputs="index_close = source('idx.1d.close')",
            formulas={
                "alpha": "factor = select_index_feature(index_close, 300)"
            },
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    assert result.arrays["alpha"].shape == (2, 2, 48)
    np.testing.assert_array_equal(result.arrays["alpha"][0], 3.0)


def test_select_index_feature_rejects_a_non_index_input() -> None:
    """验证 select_index_feature 拒绝非指数资产输入。"""
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={"stk.1d.close": np.ones((2, 2, 1))},
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs="close = source('stk.1d.close')",
            formulas={
                "alpha": "factor = select_index_feature(close, 101)"
            },
        ),
    )

    with pytest.raises(DomainError, match="requires asset type 'idx'"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


@pytest.mark.parametrize("method", ["mean", "sum", "std"])
def test_index_member_stat_reuses_the_existing_member_operator(method: str) -> None:
    """验证 index_member_stat 复用已有成员算子且不绑定资产轴。"""
    values = np.array([[[1.0], [3.0]], [[2.0], [4.0]]])
    member = np.ones_like(values)
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={"stk.1d.value": values, "stk.1d.member": member},
        input_specs={
            "stk.1d.member": InputSpec(
                "stk", "1d", 1, value_kind=ValueKind.MASK
            )
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "value = source('stk.1d.value')\n"
                "member = source('stk.1d.member')"
            ),
            formulas={
                "helper": (
                    f"factor = index_member_stat(value, member, method='{method}')"
                ),
                "direct": f"factor = member_{method}(value, member)",
            },
        ),
    )

    job = BatchFactorEngine(provider).compile(request)

    assert job.plan.outputs["helper"] == job.plan.outputs["direct"]
    term = job.plan.terms[job.plan.outputs["helper"]]
    assert isinstance(term, OperatorTerm)
    assert term.operator_name == f"member_{method}"
    assert term.layout.asset_count == 1


def test_index_member_stat_rejects_an_unknown_method_before_loading() -> None:
    """验证 index_member_stat 在加载前拒绝未知 method。"""
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": CODES},
        data={
            "stk.1d.value": np.ones((2, 2, 1)),
            "stk.1d.member": np.ones((2, 2, 1)),
        },
    )
    request = ComputeRequest(
        DomainSpec("20240102", "20240103", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "value = source('stk.1d.value')\n"
                "member = source('stk.1d.member')"
            ),
            formulas={
                "alpha": (
                    "factor = index_member_stat(value, member, method='median')"
                )
            },
        ),
    )

    with pytest.raises(CompileError, match="does not support method 'median'"):
        BatchFactorEngine(provider).compile(request)

    assert provider.load_calls == []


def test_equal_shapes_with_different_axis_identity_compute_positionally() -> None:
    """验证形状相同但资产身份不同的项按位置运算。"""
    provider = MemoryDataProvider(
        dates=DATES[:2],
        asset_codes={"stk": [1, 2], "idx": [2, 1]},
        data={"stk.1d.close": np.ones((2, 2)), "idx.1d.close": np.ones((2, 2))},
    )
    request = ComputeRequest(
        DomainSpec(
            "20240102",
            "20240103",
            {"stk": "all", "idx": "all"},
            "stk",
            "1d",
            1,
        ),
        FormulaBatch.from_text(
            common_inputs="""
                stock = source('stk.1d.close')
                index = source('idx.1d.close')
            """,
            formulas={"alpha": "factor = stock + index"},
        ),
    )

    result = BatchFactorEngine(provider).compute(request)

    np.testing.assert_array_equal(result.arrays["alpha"], 2.0)


def test_temporary_factor_repository_save_and_load_factor_round_trip(tmp_path) -> None:
    """验证临时因子仓储保存已保存因子后可按 load_factor 读回。"""
    base = _provider()
    engine = BatchFactorEngine(base)
    source_request = _request({"saved_alpha": "factor = close + 1"})
    repository = TemporaryFactorRepository(tmp_path / "factors")

    saved = repository.save(
        engine.stream(source_request, options=ExecutionOptions(chunk_size=2))
    )

    assert saved == ("saved_alpha",)
    provider = RepositoryDataProvider(base, repository)
    load_request = ComputeRequest(
        DomainSpec("20240102", "20240108", {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            formulas={"loaded": "factor = load_factor('saved_alpha')"}
        ),
    )
    loaded = BatchFactorEngine(provider).compute(load_request)
    np.testing.assert_allclose(
        loaded.arrays["loaded"],
        np.arange(1, 11, dtype=np.float64).reshape(5, 2, 1) + 1,
    )


def test_temporary_factor_repository_loads_only_overlapping_disk_chunks(
    tmp_path, monkeypatch
) -> None:
    """验证仓储按读取域只加载与其重叠的磁盘结果分块。"""
    repository = TemporaryFactorRepository(tmp_path / "factors")
    repository.save(
        BatchFactorEngine(_provider()).stream(
            _request({"saved_alpha": "factor = close + 1"}),
            options=ExecutionOptions(chunk_size=2),
        )
    )
    loaded_files = []
    original_load_chunk = repository._load_chunk

    def tracked_load_chunk(path):
        """记录实际加载的磁盘分块文件名。"""
        loaded_files.append(path.name)
        return original_load_chunk(path)

    monkeypatch.setattr(repository, "_load_chunk", tracked_load_chunk)
    values = repository.load(
        "saved_alpha",
        ReadDomain(
            ("20240104", "20240105"),
            ("20240104", "20240105"),
            (202, 101),
            (0,),
            slice(0, 2),
        ),
    )

    assert loaded_files == ["2_4.npy"]
    expected = np.arange(1, 11, dtype=np.float64).reshape(5, 2, 1) + 1
    np.testing.assert_array_equal(values, expected[2:4, ::-1, :])


def test_temporary_factor_repository_aborts_staging_on_stream_failure(tmp_path) -> None:
    """验证结果流失败时仓储中止暂存且不落盘。"""

    class FailingProvider(MemoryDataProvider):
        """在第二次加载时抛出异常以模拟提供方故障。"""

        def load_many(self, bindings):
            """第二次调用时抛出模拟故障。"""
            if self.load_calls:
                raise RuntimeError("simulated provider failure")
            return super().load_many(bindings)

    provider = FailingProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={"stk.1d.close": np.ones((5, 2))},
    )
    request = _request(
        {"alpha": "factor = close"},
        common_inputs="close = source('stk.1d.close')",
    )
    repository = TemporaryFactorRepository(tmp_path / "factors")

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        repository.save(
            BatchFactorEngine(provider).stream(
                request, options=ExecutionOptions(chunk_size=1)
            )
        )

    assert list((tmp_path / "factors").iterdir()) == []
