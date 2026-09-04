"""导出纯运算函数及其注册表。"""

from .elementwise import OperatorSpec, VariadicInput
from .registry import default_operator_registry, validate_operator_registry

__all__ = [
    "OperatorSpec",
    "VariadicInput",
    "default_operator_registry",
    "validate_operator_registry",
]
