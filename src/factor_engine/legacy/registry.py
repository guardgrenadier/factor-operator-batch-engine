"""旧版实现：特征定义的注册、规范化与持久化管理。"""

from __future__ import annotations

import json
import keyword
from dataclasses import replace
from pathlib import Path
from typing import Any

from .engine import (
    Expr,
    FormulaParser,
    _collect_raw_dependencies,
    _collect_source_specs,
    _expr_from_dict,
    _expr_to_dict,
    _mask_expr,
    normalize_registered_expr,
)
from .data.model import FeatureDef, _json_default, parse_feature_key


class FeatureRegistry:
    """管理规范化特征定义及其唯一可选别名。"""

    def __init__(self, definitions_dir: str | Path | None = None):
        """初始化定义目录和内存索引，并按需加载已有定义。"""
        self.definitions_dir = (
            Path(definitions_dir) if definitions_dir is not None else None
        )
        self._definitions: dict[str, FeatureDef] = {}
        self._alias_to_key: dict[str, str] = {}
        if self.definitions_dir is not None:
            self.load()

    @property
    def aliases(self) -> dict[str, str]:
        """返回 alias 派生索引的副本。"""
        return dict(self._alias_to_key)

    def register(self, feature_def: FeatureDef) -> FeatureDef:
        """规范化并注册一个尚不存在的定义。"""
        # 先完成表达式和依赖规范化，再原子更新定义与别名索引。
        normalized = self._normalize(feature_def)
        if normalized.key in self._definitions:
            raise KeyError(f"FeatureDef {normalized.key!r} is already registered")
        self._validate_alias(normalized.alias, normalized.key)
        self._definitions[normalized.key] = normalized
        if normalized.alias is not None:
            self._alias_to_key[normalized.alias] = normalized.key
        return normalized

    def replace(self, feature_def: FeatureDef) -> FeatureDef:
        """原子替换已有定义及其 alias 派生索引。"""
        # 校验新定义后再移除旧别名，避免失败时破坏现有状态。
        key = parse_feature_key(feature_def.key).key
        if key not in self._definitions:
            raise KeyError(f"FeatureDef {key!r} is not registered")
        normalized = self._normalize(feature_def)
        self._validate_alias(normalized.alias, key)
        old = self._definitions[key]
        if old.alias is not None:
            self._alias_to_key.pop(old.alias, None)
        self._definitions[key] = normalized
        if normalized.alias is not None:
            self._alias_to_key[normalized.alias] = key
        return normalized

    def remove(self, key: str) -> FeatureDef:
        """删除一个 canonical definition 及其 alias 索引。"""
        canonical = parse_feature_key(key).key
        if canonical not in self._definitions:
            raise KeyError(f"FeatureDef {canonical!r} is not registered")
        feature_def = self._definitions.pop(canonical)
        if feature_def.alias is not None:
            self._alias_to_key.pop(feature_def.alias, None)
        return feature_def

    def get(self, key: str) -> FeatureDef:
        """按 canonical key 获取定义，不执行 alias 或 Store fallback。"""
        canonical = parse_feature_key(key).key
        try:
            return self._definitions[canonical]
        except KeyError as exc:
            raise KeyError(f"FeatureDef {canonical!r} is not registered") from exc

    def resolve_key(self, key_or_alias: str) -> str:
        """将 canonical key 或 alias 解析为已注册 canonical key。"""
        candidate = self._alias_to_key.get(key_or_alias, key_or_alias)
        try:
            canonical = parse_feature_key(candidate).key
        except ValueError as exc:
            raise KeyError(f"Unknown feature key or alias {key_or_alias!r}") from exc
        if canonical not in self._definitions:
            raise KeyError(f"FeatureDef {key_or_alias!r} is not registered")
        return canonical

    def resolve(self, key_or_alias: str) -> FeatureDef:
        """按 canonical key 或 alias 获取定义。"""
        return self._definitions[self.resolve_key(key_or_alias)]

    def contains(self, key: str) -> bool:
        """判断 canonical key 是否已注册。"""
        try:
            canonical = parse_feature_key(key).key
        except ValueError:
            return False
        return canonical in self._definitions

    def add_alias(self, key: str, alias: str) -> FeatureDef:
        """为当前没有 alias 的 definition 增加 alias。"""
        feature_def = self.get(key)
        if feature_def.alias is not None:
            raise KeyError(
                f"FeatureDef {feature_def.key!r} already has alias {feature_def.alias!r}"
            )
        return self._set_alias(feature_def, alias)

    def update_alias(self, key: str, alias: str) -> FeatureDef:
        """修改 definition 已有的 alias。"""
        feature_def = self.get(key)
        if feature_def.alias is None:
            raise KeyError(f"FeatureDef {feature_def.key!r} does not have an alias")
        return self._set_alias(feature_def, alias)

    def remove_alias(self, key: str) -> FeatureDef:
        """删除 definition 已有的 alias。"""
        feature_def = self.get(key)
        if feature_def.alias is None:
            raise KeyError(f"FeatureDef {feature_def.key!r} does not have an alias")
        self._alias_to_key.pop(feature_def.alias, None)
        updated = replace(feature_def, alias=None)
        self._definitions[feature_def.key] = updated
        return updated

    def list(self, *, asset: str | None = None, freq: str | None = None) -> list[str]:
        """按可选资产和频率筛选定义键。"""
        keys = sorted(self._definitions)
        if asset is not None:
            keys = [key for key in keys if parse_feature_key(key).asset == asset]
        if freq is not None:
            keys = [key for key in keys if parse_feature_key(key).freq == freq]
        return keys

    def search(self, text: str) -> list[FeatureDef]:
        """在已注册定义的序列化内容中搜索文本。"""
        query = text.lower()
        return [
            feature_def
            for feature_def in self._definitions.values()
            if query in json.dumps(feature_def.to_dict(), default=_json_default).lower()
        ]

    def save(self, root: str | Path | None = None) -> None:
        """保存当前 definitions；alias 随 FeatureDef 保存，不写独立索引文件。"""
        root_path = self._root(root)
        expected: set[Path] = set()
        # 按资产和频率分目录写出当前全部定义。
        for feature_def in self._definitions.values():
            fk = parse_feature_key(feature_def.key)
            path = root_path / fk.asset / fk.freq / f"{fk.name}.json"
            expected.add(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as file:
                json.dump(
                    feature_def.to_dict(),
                    file,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                )
        # 删除已不在注册表中的陈旧定义文件。
        for path in root_path.glob("*/*/*.json"):
            if path not in expected:
                path.unlink()

    def load(self, root: str | Path | None = None) -> None:
        """从 definition 文件加载并整体替换当前 Registry 状态。"""
        root_path = self._root(root)
        if not root_path.exists():
            return
        # 暂存旧状态，使任一文件加载失败时可以整体回滚。
        old_definitions = self._definitions
        old_aliases = self._alias_to_key
        self._definitions = {}
        self._alias_to_key = {}
        try:
            for path in sorted(root_path.glob("*/*/*.json")):
                with path.open("r", encoding="utf-8") as file:
                    self.register(FeatureDef.from_dict(json.load(file)))
        except Exception:
            self._definitions = old_definitions
            self._alias_to_key = old_aliases
            raise

    def _normalize(self, feature_def: FeatureDef) -> FeatureDef:
        """校验并规范化特征定义中的表达式、掩码、依赖和延迟配置。"""
        # 定义字段必须与规范化后的特征键完全一致。
        fk = parse_feature_key(feature_def.key)
        if (feature_def.asset, feature_def.freq, feature_def.name) != (
            fk.asset,
            fk.freq,
            fk.name,
        ):
            raise ValueError(
                f"FeatureDef fields do not match key {fk.key!r}: "
                f"{feature_def.asset!r}, {feature_def.freq!r}, {feature_def.name!r}"
            )
        # 解析主公式，并把其中的别名替换成规范键。
        formula = self._parse_expr(
            feature_def.formula, label=f"FeatureDef {fk.key!r} formula"
        )
        formula = normalize_registered_expr(formula, self._alias_to_key)
        # 三类掩码使用与主公式相同的表达式规范化规则。
        masks = tuple(
            self._normalize_mask(mask)
            for mask in (
                feature_def.input_mask,
                feature_def.sample_mask,
                feature_def.output_mask,
            )
        )
        # 汇总主公式及掩码引用的依赖和外部数据源。
        dependencies = list(_collect_raw_dependencies(formula))
        source_specs = list(_collect_source_specs(formula))
        for mask in masks:
            if mask is None:
                continue
            expr = _expr_from_dict(mask)
            dependencies.extend(_collect_raw_dependencies(expr))
            source_specs.extend(_collect_source_specs(expr))
        # 同一数据源键不得携带相互冲突的读取参数。
        sources_by_key: dict[str, Any] = {}
        for spec in source_specs:
            existing = sources_by_key.get(spec.key)
            if existing is not None and existing != spec:
                raise ValueError(
                    f"Source key {spec.key!r} is used with conflicting SourceSpec values"
                )
            sources_by_key[spec.key] = spec
        # 规范化延迟键，并限制其只能引用实际公式输入。
        delay_dict = {
            self._normalize_delay_key(key): int(value)
            for key, value in feature_def.delay_dict.items()
        }
        allowed_delay_keys = {*dependencies, *sources_by_key}
        unknown_delay_keys = sorted(set(delay_dict) - allowed_delay_keys)
        if unknown_delay_keys:
            raise ValueError(
                f"FeatureDef {fk.key!r} delay_dict keys are not formula inputs: {unknown_delay_keys}"
            )
        return replace(
            feature_def,
            formula=_expr_to_dict(formula),
            dependencies=tuple(dict.fromkeys(dependencies)),
            input_mask=masks[0],
            sample_mask=masks[1],
            output_mask=masks[2],
            delay_dict=delay_dict,
        )

    def _parse_expr(self, value: Any, *, label: str) -> Expr:
        """将字典、表达式对象或公式字符串统一解析为表达式。"""
        if isinstance(value, dict):
            if "type" not in value:
                raise ValueError(f"{label} dict requires type")
            return _expr_from_dict(value)
        if isinstance(value, Expr):
            return value
        if isinstance(value, str):
            return FormulaParser().parse(value)
        raise ValueError(f"{label} is required")

    def _normalize_mask(self, mask: Any | None) -> dict[str, Any] | None:
        """将可选掩码规范化为可序列化的表达式字典。"""
        if mask is None:
            return None
        expr = _expr_from_dict(mask) if isinstance(mask, dict) else _mask_expr(mask)
        return _expr_to_dict(normalize_registered_expr(expr, self._alias_to_key))

    def _normalize_delay_key(self, key_or_alias: str) -> str:
        """将延迟配置中的特征键或别名规范化为完整特征键。"""
        candidate = self._alias_to_key.get(key_or_alias, key_or_alias)
        try:
            return parse_feature_key(candidate).key
        except ValueError as exc:
            raise KeyError(f"Unknown delay_dict key or alias {key_or_alias!r}") from exc

    def _set_alias(self, feature_def: FeatureDef, alias: str) -> FeatureDef:
        """校验并替换指定特征的别名及其派生索引。"""
        self._validate_alias(alias, feature_def.key)
        if feature_def.alias is not None:
            self._alias_to_key.pop(feature_def.alias, None)
        updated = replace(feature_def, alias=alias)
        self._definitions[feature_def.key] = updated
        self._alias_to_key[alias] = feature_def.key
        return updated

    def _validate_alias(self, alias: str | None, key: str) -> None:
        """校验别名格式及其在注册表中的唯一性。"""
        if alias is None:
            return
        if not alias.isidentifier() or keyword.iskeyword(alias):
            raise ValueError(f"Alias {alias!r} must be a non-keyword identifier")
        owner = self._alias_to_key.get(alias)
        if owner is not None and owner != key:
            raise KeyError(f"Alias {alias!r} is already registered for {owner!r}")

    def _root(self, root: str | Path | None) -> Path:
        """解析本次读写应使用的定义根目录。"""
        if root is not None:
            return Path(root)
        if self.definitions_dir is None:
            raise ValueError("A definitions directory is required")
        return self.definitions_dir
