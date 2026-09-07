"""旧版特征注册表的别名、依赖推导与持久化测试（沿用旧版术语）。"""

from __future__ import annotations

import json

import pytest

from factor_engine.legacy import FeatureRegistry, SourceExpr, get_fund
from factor_engine.legacy.data.model import ExecutionRequest, FeatureDef
from factor_engine.legacy.engine import FeatureExpr, _expr_from_dict


def test_registry_freezes_alias_and_derives_dependencies() -> None:
    """验证注册表冻结别名并从公式推导依赖项。"""
    registry = FeatureRegistry()
    registry.register(
        FeatureDef.from_key(
            "stk.1d.base",
            alias="base",
            formula="add(stk.1d.raw, 1)",
        )
    )
    derived = registry.register(
        FeatureDef.from_key(
            "stk.1d.derived",
            formula="multiply(base, stk.1d.future_input)",
        )
    )

    assert derived.dependencies == ("stk.1d.base", "stk.1d.future_input")
    expr = _expr_from_dict(derived.formula)
    assert isinstance(expr.args[0], FeatureExpr)
    assert expr.args[0].key == "stk.1d.base"

    registry.update_alias("stk.1d.base", "renamed_base")

    assert registry.resolve_key("renamed_base") == "stk.1d.base"
    with pytest.raises(KeyError):
        registry.resolve_key("base")
    assert registry.get("stk.1d.derived") == derived


def test_registry_alias_operations_update_definition_atomically() -> None:
    """验证别名增删改原子地更新对应定义。"""
    registry = FeatureRegistry()
    registry.register(FeatureDef.from_key("stk.1d.a", formula="stk.1d.raw_a"))
    registry.register(
        FeatureDef.from_key(
            "stk.1d.b",
            alias="b",
            formula="stk.1d.raw_b",
        )
    )

    updated = registry.add_alias("stk.1d.a", "a")
    assert updated.alias == "a"
    assert registry.resolve_key("a") == "stk.1d.a"

    with pytest.raises(KeyError):
        registry.update_alias("stk.1d.a", "b")
    assert registry.resolve_key("a") == "stk.1d.a"
    assert registry.get("stk.1d.a").alias == "a"

    removed = registry.remove_alias("stk.1d.a")
    assert removed.alias is None
    with pytest.raises(KeyError):
        registry.resolve_key("a")


def test_registry_replace_removes_stale_alias() -> None:
    """验证替换定义时移除旧别名并保留新别名。"""
    registry = FeatureRegistry()
    registry.register(
        FeatureDef.from_key(
            "stk.1d.a",
            alias="old_alias",
            formula="stk.1d.raw",
        )
    )

    replacement = registry.replace(
        FeatureDef.from_key(
            "stk.1d.a",
            alias="new_alias",
            formula="add(stk.1d.raw, 1)",
        )
    )

    assert replacement.alias == "new_alias"
    assert registry.resolve_key("new_alias") == "stk.1d.a"
    with pytest.raises(KeyError):
        registry.resolve_key("old_alias")


def test_fundamental_definition_uses_source_expr_not_dependency_flag() -> None:
    """验证基础数据源定义使用源表达式而非依赖标志。"""
    registry = FeatureRegistry()
    definition = registry.register(
        get_fund(
            "Revenue",
            column_name="value",
            quarters=4,
            name="revenue_4q",
        )
    )

    expr = _expr_from_dict(definition.formula)

    assert isinstance(expr, SourceExpr)
    assert definition.dependencies == ()
    assert expr.spec.source == "Fundamental"
    assert expr.spec.params["quarters"] == 4
    assert expr.key == "stk.1d.Revenue_value_4Q"


def test_registry_persists_alias_only_inside_definition(tmp_path) -> None:
    """验证别名只随定义持久化且不产生独立别名文件。"""
    root = tmp_path / "definitions"
    registry = FeatureRegistry(root)
    registry.register(
        FeatureDef.from_key(
            "stk.1d.a",
            alias="a",
            formula="stk.1d.raw",
        )
    )
    registry.save()

    assert not (root / "aliases.json").exists()
    payload = json.loads((root / "stk" / "1d" / "a.json").read_text(encoding="utf-8"))
    assert payload["alias"] == "a"

    restored = FeatureRegistry(root)
    assert restored.resolve_key("a") == "stk.1d.a"


def test_registry_rejects_inconsistent_feature_key_fields() -> None:
    """验证注册表拒绝特征键字段与内容不一致的定义。"""
    registry = FeatureRegistry()

    with pytest.raises(ValueError, match="do not match"):
        registry.register(
            FeatureDef(
                key="stk.1d.a",
                asset="cb",
                freq="1d",
                name="a",
                formula="stk.1d.raw",
            )
        )


def test_execution_request_rejects_options_that_cannot_take_effect() -> None:
    """验证执行请求拒绝相互矛盾而无法生效的选项。"""
    with pytest.raises(ValueError, match="overwrite"):
        ExecutionRequest(
            target="stk.1d.a",
            materialize=False,
            overwrite=True,
        )
    with pytest.raises(ValueError, match="overlap"):
        ExecutionRequest(
            target="stk.1d.a",
            overlap=2,
        )
