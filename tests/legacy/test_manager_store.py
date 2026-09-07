"""旧版特征管理器与特征存储的物化、依赖与分块执行测试（沿用旧版术语）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.legacy import DataRouter, FeatureManager, get_fund
from factor_engine.legacy.data.alignment import feature_array
from factor_engine.legacy.data.model import ExecutionRequest, FeatureDef


class EmptySourceDirectoryReader:
    """在测试 DataRouter 内存数据源时屏蔽外部数据源目录扫描。"""

    def _read_sql(self, sql: str) -> pd.DataFrame:
        """仅接受源目录查询并返回空结果。"""
        if "Fundamental_ItemCode" in sql:
            return pd.DataFrame(columns=["field", "name_cn"])
        raise AssertionError(f"Unexpected SQL in test: {sql}")


class RecordingFundamentalReader(EmptySourceDirectoryReader):
    """记录被请求的基础数据源规格并返回固定数值的读取器。"""

    def __init__(self) -> None:
        """初始化规格记录列表。"""
        self.specs = []

    def read_source(self, spec, store, *, scope=None):
        """记录源请求并返回顺序数值的特征数组。"""
        self.specs.append(spec)
        values = np.arange(6, dtype=float).reshape(3, 2)
        return feature_array(spec, store, values, scope=scope)


def test_manager_materializes_memory_source_with_output_mask(store) -> None:
    """验证管理器按输出掩码物化内存源特征。"""
    router = DataRouter(
        source_config={"source_tables": [], "sources": {}},
        reader=EmptySourceDirectoryReader(),
        memory_data={
            "stk.1d.raw": np.array(
                [
                    [1.0, 3.0],
                    [2.0, 4.0],
                    [3.0, 5.0],
                ]
            )
        },
    )
    manager = FeatureManager(store, data_router=router)
    manager.registry.register(
        FeatureDef.from_key(
            "stk.1d.alpha",
            alias="alpha",
            formula="cs_zscore(stk.1d.raw)",
            output_mask="greater(stk.1d.raw, 1)",
        )
    )

    feature = manager.execute(ExecutionRequest(target="alpha"))

    expected = np.array(
        [
            [[np.nan], [1.0]],
            [[-1.0], [1.0]],
            [[-1.0], [1.0]],
        ]
    )
    np.testing.assert_allclose(feature.values, expected, equal_nan=True)
    np.testing.assert_allclose(
        store.load_feature("stk.1d.alpha").values, expected, equal_nan=True
    )


def test_store_import_array_round_trip(store) -> None:
    """验证特征存储导入与读回数组的往返一致性。"""
    original = np.array(
        [
            [1.0, np.nan],
            [2.0, 4.0],
            [3.0, 5.0],
        ]
    )

    store.import_array("stk.1d.imported", original)
    restored = store.load_feature("stk.1d.imported")

    np.testing.assert_allclose(restored.values[:, :, 0], original, equal_nan=True)


def test_manager_materializes_registered_dependency(store) -> None:
    """验证管理器按需物化已注册依赖并仅保存目标特征。"""
    router = DataRouter(
        source_config={"source_tables": [], "sources": {}},
        reader=EmptySourceDirectoryReader(),
        memory_data={"stk.1d.raw": np.ones((3, 2))},
    )
    manager = FeatureManager(store, data_router=router)
    manager.registry.register(
        FeatureDef.from_key("stk.1d.base", formula="add(stk.1d.raw, 1)")
    )
    manager.registry.add_alias("stk.1d.base", "base")
    manager.registry.register(
        FeatureDef.from_key(
            "stk.1d.derived",
            formula="multiply(base, 2)",
        )
    )

    feature = manager.execute(ExecutionRequest(target="stk.1d.derived"))

    assert not store.has_feature("stk.1d.base")
    assert store.has_feature("stk.1d.derived")
    np.testing.assert_allclose(feature.values, np.full((3, 2, 1), 4.0))


def test_manager_executes_fundamental_source_expr_without_bridge(store) -> None:
    """验证管理器直接执行基础数据源表达式而不经桥接。"""
    reader = RecordingFundamentalReader()
    router = DataRouter(
        source_config={"source_tables": [], "sources": {}},
        reader=reader,
    )
    manager = FeatureManager(store, data_router=router)
    definition = manager.registry.register(
        get_fund(
            "Revenue",
            column_name="value",
            name="revenue",
        )
    )

    result = manager.execute(
        ExecutionRequest(
            target=definition.key,
            materialize=False,
        )
    )

    np.testing.assert_allclose(result.values[:, :, 0], np.arange(6).reshape(3, 2))
    assert len(reader.specs) == 1
    assert reader.specs[0].source == "Fundamental"
    assert router.source_overrides == {}
    assert not store.has_feature(definition.key)


def test_source_expr_reuses_materialized_raw_source_before_router(store) -> None:
    """验证源表达式优先复用已物化原始数据而不走路由读取。"""
    raw = np.arange(6, dtype=float).reshape(3, 2)
    store.import_array("stk.1d.Revenue_value", raw)
    reader = RecordingFundamentalReader()
    manager = FeatureManager(
        store,
        data_router=DataRouter(
            source_config={"source_tables": [], "sources": {}},
            reader=reader,
        ),
    )
    definition = manager.registry.register(
        get_fund(
            "Revenue",
            column_name="value",
            name="revenue_copy",
        )
    )

    result = manager.execute(
        ExecutionRequest(
            target=definition.key,
            materialize=False,
        )
    )

    np.testing.assert_allclose(result.values[:, :, 0], raw)
    assert reader.specs == []


def test_chunked_execution_matches_full_execution(store) -> None:
    """验证分块执行结果与整段执行结果一致。"""
    router = DataRouter(
        source_config={"source_tables": [], "sources": {}},
        reader=EmptySourceDirectoryReader(),
        memory_data={"stk.1d.raw": np.arange(6, dtype=float).reshape(3, 2)},
    )
    manager = FeatureManager(store, data_router=router)
    manager.registry.register(
        FeatureDef.from_key(
            "stk.1d.chunked",
            formula="add(stk.1d.raw, 1)",
        )
    )

    full = manager.execute(
        ExecutionRequest(
            target="stk.1d.chunked",
            materialize=False,
        )
    )
    chunked = manager.execute(
        ExecutionRequest(
            target="stk.1d.chunked",
            chunk_size=2,
        )
    )

    np.testing.assert_allclose(chunked.values, full.values)
