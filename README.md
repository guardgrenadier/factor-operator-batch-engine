# Factor Engine

正式架构和完整契约见 [`FACTOR_ENGINE_DESIGN.md`](FACTOR_ENGINE_DESIGN.md)。
本文件只说明当前推荐入口。

> 当前代码仍在从 `TermDomain/domain_rule` 迁移到普通算子按位置计算的
> `ArrayLayout` 契约；本 README 中描述旧校验行为的部分代表当前实现，不代表
> 已接受的目标设计。决策见
> [`ADR-0001`](docs/adr/0001-use-positional-array-layout-for-operators.md)。

## 包结构

```text
src/factor_engine/
├── formula.py       # AST、Parser、FormulaBatch 与名称绑定
├── domain.py        # ValueKind、频率、日期与稳定轴身份
├── model.py         # Domain、Term、Plan、Request 等引擎契约
├── compiler.py      # helper 展开、domain lowering、CSE、lookback
├── execution.py     # PhysicalPlanner、Runtime、ResultStream、Engine
├── providers.py     # MemoryDataProvider（契约测试与小型研究）
├── data_provider/   # 正式 Catalog、Backend、批量 Reader、Normalizer 与 Provider
├── repository.py    # 临时因子 staging/commit/load 闭环
├── operators/       # elementwise、timeseries、cross-section、alignment
└── legacy/          # 旧研究层与旧数据设施（Store/Router/Reader），仅供追溯
```

顶层 `factor_engine` 只暴露新管线的稳定入口；旧研究层（含旧 Snapshot
Store、DataRouter 与 SmartQuant Reader）必须从 `factor_engine.legacy`
显式导入。取数链路三代演进见
[`docs/取数链路演进归档.md`](docs/取数链路演进归档.md)。

## 最小示例

```python
import numpy as np

from factor_engine import (
    BatchFactorEngine,
    ComputeRequest,
    DomainSpec,
    ExecutionOptions,
    FormulaBatch,
    MemoryDataProvider,
)

provider = MemoryDataProvider(
    dates=["20240102", "20240103", "20240104"],
    asset_codes={"stk": [101, 202]},
    data={
        "stk.1d.close": np.ones((3, 2)),
        "stk.1d.volume": np.full((3, 2), 10.0),
    },
    load_groups={
        "stk.1d.close": "daily_quotes",
        "stk.1d.volume": "daily_quotes",
    },
)

batch = FormulaBatch.from_text(
    common_inputs="""
        close = source("stk.1d.close")
        volume = source("stk.1d.volume")
    """,
    formulas={
        "alpha_1": """
            mean = ts_mean(close, 2)
            factor = mean / volume
        """,
        "alpha_2": "factor = (close + volume) * 2",
    },
)

request = ComputeRequest(
    domain=DomainSpec(
        start="20240102",
        end="20240104",
        asset_scope={"stk": "all"},
        target_asset="stk",
        target_freq="1d",
        target_step_count=1,
    ),
    batch=batch,
)

engine = BatchFactorEngine(provider)
result = engine.compute(request, options=ExecutionOptions(chunk_size=2))

alpha = result.arrays["alpha_1"]       # date × asset × step
frame = result.to_dataframe()           # MultiIndex(date, asset, step)
```

`engine.compute()` 只负责完整消费 `engine.stream()`；需要边算边消费时直接使用：

```python
for chunk in engine.stream(request):
    consume(chunk.formula_id, chunk.output_slice, chunk.values)
```

chunk 在流自然结束前都只是 provisional 结果。

## FormulaBatch 作用域

- `common_inputs` 是所有公式共享的顺序绑定程序，自身不产生输出；
- 每个 formula 是独立的顺序绑定程序，最后一个 binding 是该 formula 的输出；
- 两者都可以声明 Source、operator 表达式以及顺序中间变量；
- formula 可引用 `common_inputs` 和本组前面定义的局部名称；
- 禁止前向引用、跨 formula 局部引用、重复定义和覆盖 common input；
- 名称不参与 DAG 结构身份，相同表达式会跨公式 CSE。

复杂指标可以在 `common_inputs` 中只定义一次，再由多个因子做不同统计：

```python
batch = FormulaBatch.from_text(
    common_inputs="""
        close = get_lf("stk", "ClosePrice")
        volume = get_lf("stk", "TurnoverVolume")
        raw_indicator = close / volume
        indicator = ts_mean(raw_indicator, 10)
    """,
    formulas={
        "indicator_std_20": "factor = ts_std(indicator, 20)",
        "indicator_std_5": "factor = ts_std(indicator, 5)",
        "indicator_mean_20": "factor = ts_mean(indicator, 20)",
    },
)
```

`indicator` 对应的表达式会进入共享 Term DAG，并被多个输出复用。未被任何输出引用的
common input 不进入 LogicalPlan，也不会触发数据加载。Source 也可以只在某个 formula
内部声明；此时它只在该 formula 的局部作用域可见。

Python 代码可从 `factor_engine.formula` 导入 `source/get_lf/get_hf/get_fund/load_factor/operator`
直接构造不可变 AST；这些 helper 不读取数据或修改全局状态。

## 显式重采样

频率不会隐式转换。对任意表达式从细频率聚合到粗频率时，直接使用公开
`resample` operator：

```python
daily_mean = operator("resample", intraday_value, "1d", method="mean")
```

直接取高频 Source 时，`get_hf` 提供等价的简写：

```python
close_15m = get_hf(
    "stk",
    "1min",
    "ClosePrice",
    resample="15min",
    method="last",
)
```

它等价于：

```python
close_15m = operator(
    "resample",
    get_hf("stk", "1min", "ClosePrice"),
    "15min",
    method="last",
)
```

两种写法都会形成同一个公开 `OperatorExpr("resample")`，并使用默认 Operator Registry
中的同一个 `resample` kernel；DataProvider 始终只描述和加载原始 `1min` Source。首版支持
`mean/sum/std/last`，并要求同时显式给出目标频率和 `method`。

## 数据和 Domain

- `asset_scope={"stk": "all"}` 使用任务开始时 provider 提供的完整有序 master axis；
- 显式子集保留调用方顺序，并拒绝重复或未知代码；
- Runtime 数组统一为 `float64`，缺失值为 `NaN`，普通 Term shape 为 `T × N × S`；
- 日频到日内由 Compiler 插入 singleton step broadcast；
- 细频率到粗频率必须写 `resample(expr, "1d", method="mean")`；
- `stk → cb` 写 `project_stk_to_cb(values)`；helper 会自动注册任务级 mapping Source；
- 指数 Source 由用户显式声明，再用 `select_index_feature(values, index)` 选择单个指数；
- 指数成员池统计使用 `index_member_stat(values, member, method=...)`；
- shape 相同但坐标身份不同仍会编译失败。

真实数据任务为每个任务创建一个独立 Provider：

```python
from factor_engine import BatchFactorEngine, SmartQuantDataProvider

provider = SmartQuantDataProvider()  # 从环境变量或仓库根目录 .env 读取 OB 配置
engine = BatchFactorEngine(provider)
result = engine.compute(request)
```

它通过独立的 Catalog、OceanBase/DuckDB backend 和批量 dataset reader 按任务解析
`data_sources.json` 与数据库 catalog，并在“输出区间 + 公式 lookback”内冻结
calendar 和资产轴。它不依赖旧 Store、Router、Reader 或 FeatureArray。同一物理表
的多个字段由一次 SQL 或 parquet scan 批量读取；物理 I/O 事件可从
`result.stats.provider_events` 查看。旧 Snapshot 链路的迁移背景见
[`docs/取数链路演进归档.md`](docs/取数链路演进归档.md)。

物理 `SourceSpec` 只在每个分区的 `bind_many()` 阶段产生，不进入 LogicalPlan 语义身份。

## 临时保存闭环

正式 FactorRepository 尚未设计。当前只有验证语义用的临时实现：

```python
from factor_engine import RepositoryDataProvider, TemporaryFactorRepository

repository = TemporaryFactorRepository("temporary_factors")
repository.save(engine.stream(request))

read_provider = RepositoryDataProvider(provider, repository)
# 之后公式可使用 load_factor("alpha_1")
```

保存先写 staging；流自然完成后提交，异常时删除 staging。该目录格式不是未来正式仓库标准。

## 旧研究层

`Calculator / FeatureManager / FeatureRegistry` 以及旧数据设施
`FeatureStore / DataRouter / SmartQuantSourceReader` 均位于
`factor_engine.legacy`，仅供既有实验代码追溯，不从顶层公共 API 暴露。
后续研究层应建立在上述 `FormulaBatch + ComputeRequest` 契约之上。
