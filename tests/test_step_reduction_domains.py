"""覆盖日内 step 归约算子在逻辑计划中的 Term Domain 推导与对齐的测试。"""

from __future__ import annotations

import numpy as np
import pytest

from factor_engine import (
    BatchFactorEngine,
    ComputeRequest,
    DomainSpec,
    FormulaBatch,
    MemoryDataProvider,
    OperatorTerm,
)


DATES = [f"202401{day:02d}" for day in range(1, 26)]
CODES = [11, 22]


def _request(
    formulas: dict[str, str],
    *,
    source_freq: str,
    target_freq: str,
    target_step_count: int,
) -> ComputeRequest:
    """构造共享公共输入 x 的计算请求，指定源与目标频率和 step 数。"""
    return ComputeRequest(
        DomainSpec(
            DATES[0],
            DATES[-1],
            {"stk": "all"},
            "stk",
            target_freq,
            target_step_count,
        ),
        FormulaBatch.from_text(
            common_inputs=f"x = source('stk.{source_freq}.x')",
            formulas=formulas,
        ),
    )


def _provider(freq: str, step_count: int) -> MemoryDataProvider:
    """构造给定频率与 step 数的内存数据提供方，数据为顺序递增数组。"""
    values = np.arange(
        1,
        len(DATES) * len(CODES) * step_count + 1,
        dtype=np.float64,
    ).reshape(len(DATES), len(CODES), step_count)
    return MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={f"stk.{freq}.x": values},
    )


@pytest.mark.parametrize(
    ("operator", "expression"),
    [
        ("step_mean", "step_mean(x)"),
        ("step_sum", "step_sum(x)"),
        ("step_std", "step_std(x)"),
        ("step_max", "step_max(x)"),
        ("step_min", "step_min(x)"),
        ("step_first", "step_first(x)"),
        ("step_last", "step_last(x)"),
        ("step_kurtosis", "step_kurtosis(x)"),
        ("step_corr", "step_corr(x, x)"),
        ("intraday_flat_mean", "intraday_flat_mean(x, window_days=2)"),
        ("intraday_flat_std", "intraday_flat_std(x, window_days=2)"),
    ],
)
def test_intraday_step_reductions_lower_to_daily_domain(
    operator: str, expression: str
) -> None:
    """验证各类日内 step 归约算子的输出 Term Domain 为日频单 step。"""
    job = BatchFactorEngine(_provider("1min", 237)).compile(
        _request(
            {"alpha": f"factor = {expression}"},
            source_freq="1min",
            target_freq="1d",
            target_step_count=1,
        )
    )

    term = job.plan.terms[job.plan.outputs["alpha"]]

    assert isinstance(term, OperatorTerm)
    assert term.operator_name == operator
    assert term.domain is not None
    assert term.domain.frequency == "1d"
    assert term.domain.step_count == 1


def test_five_minute_step_reduction_lowers_to_daily_domain() -> None:
    """验证 5 分钟频率的 step 归约同样降低为日频单 step 域。"""
    job = BatchFactorEngine(_provider("5min", 48)).compile(
        _request(
            {"alpha": "factor = step_std(x)"},
            source_freq="5min",
            target_freq="1d",
            target_step_count=1,
        )
    )

    term = job.plan.terms[job.plan.outputs["alpha"]]

    assert term.domain is not None
    assert term.domain.frequency == "1d"
    assert term.domain.step_count == 1


def test_step_corr_merges_daily_singleton_before_lowering() -> None:
    """验证 step_corr 先合并日频单 step 输入再降低为日频域。"""
    intraday = np.ones((len(DATES), len(CODES), 48), dtype=np.float64)
    daily = np.ones((len(DATES), len(CODES), 1), dtype=np.float64)
    provider = MemoryDataProvider(
        dates=DATES,
        asset_codes={"stk": CODES},
        data={"stk.5min.x": intraday, "stk.1d.y": daily},
    )
    request = ComputeRequest(
        DomainSpec(DATES[0], DATES[-1], {"stk": "all"}, "stk", "1d", 1),
        FormulaBatch.from_text(
            common_inputs=(
                "x = source('stk.5min.x')\n"
                "y = source('stk.1d.y')"
            ),
            formulas={"alpha": "factor = step_corr(x, y)"},
        ),
    )

    job = BatchFactorEngine(provider).compile(request)
    term = job.plan.terms[job.plan.outputs["alpha"]]

    assert term.domain is not None
    assert term.domain.frequency == "1d"
    assert term.domain.step_count == 1


def test_date_rolling_preserves_daily_step_reduction_domain() -> None:
    """验证日频滚动算子不改变 step 归约得到的日频单 step 域。"""
    job = BatchFactorEngine(_provider("1min", 237)).compile(
        _request(
            {"alpha": "factor = ts_mean(step_kurtosis(x), window=20)"},
            source_freq="1min",
            target_freq="1d",
            target_step_count=1,
        )
    )

    term = job.plan.terms[job.plan.outputs["alpha"]]

    assert isinstance(term, OperatorTerm)
    assert term.operator_name == "ts_mean"
    assert term.domain is not None
    assert term.domain.frequency == "1d"
    assert term.domain.step_count == 1


def test_step_reduction_chain_compiles_and_executes_as_daily_factor() -> None:
    """验证 step 归约链式公式能编译并执行出日频因子。"""
    result = BatchFactorEngine(_provider("1min", 237)).compute(
        _request(
            {
                "alpha": (
                    "minute_ret = step_pct_change(x)\n"
                    "daily_kurt = step_kurtosis(minute_ret)\n"
                    "factor = ts_mean(daily_kurt, window=20)"
                )
            },
            source_freq="1min",
            target_freq="1d",
            target_step_count=1,
        )
    )

    assert result.arrays["alpha"].shape == (len(DATES), len(CODES), 1)
    output = result.plan.terms[result.plan.outputs["alpha"]]
    assert output.domain is not None
    assert output.domain.frequency == "1d"
    assert output.domain.step_count == 1


def test_get_step_preserves_intraday_frequency() -> None:
    """验证 get_step 保留日内频率并返回单日 step。"""
    job = BatchFactorEngine(_provider("1min", 237)).compile(
        _request(
            {"alpha": "factor = get_step(x, step=0)"},
            source_freq="1min",
            target_freq="1min",
            target_step_count=1,
        )
    )

    term = job.plan.terms[job.plan.outputs["alpha"]]

    assert term.domain is not None
    assert term.domain.frequency == "1min"
    assert term.domain.step_count == 1


def test_daily_step_reduction_broadcasts_to_intraday_target() -> None:
    """验证日频 step 归约结果在日内目标域上按值广播。"""
    result = BatchFactorEngine(_provider("1min", 237)).compute(
        _request(
            {"alpha": "factor = step_mean(x)"},
            source_freq="1min",
            target_freq="1min",
            target_step_count=237,
        )
    )

    values = result.arrays["alpha"]
    assert values.shape == (len(DATES), len(CODES), 237)
    np.testing.assert_array_equal(
        values, np.broadcast_to(values[:, :, :1], values.shape)
    )
    output = result.plan.terms[result.plan.outputs["alpha"]]
    assert output.domain is not None
    assert output.domain.frequency == "1d"
    assert output.domain.step_count == 1
