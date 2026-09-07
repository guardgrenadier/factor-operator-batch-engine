"""为拆分后的数据层提供兼容导出，新代码应直接使用具体子模块。"""

from .router import DataRouter, SmartQuantDataRouter
from .smartquant import SmartQuantSourceReader
from .sources import (
    default_source_config,
    fundamental_name,
    load_source_config,
    minute_data_type,
    minute_path,
)

__all__ = [
    "DataRouter",
    "SmartQuantDataRouter",
    "SmartQuantSourceReader",
    "default_source_config",
    "fundamental_name",
    "load_source_config",
    "minute_data_type",
    "minute_path",
]
