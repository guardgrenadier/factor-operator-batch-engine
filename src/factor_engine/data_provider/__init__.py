"""新 batch engine 的任务级数据访问组件与无 I/O 配置校验入口。"""

from .catalog import load_config, validate_config

__all__ = ["load_config", "validate_config"]
