"""
应用主配置

Mini Agent 的核心配置类，使用单例模式和热更新。
"""
import logging
import os
from pathlib import Path
from typing import Any, Self

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .model_config import ModelConfig
from .paths import Paths

load_dotenv()

logger = logging.getLogger(__name__)


class AppConfig(BaseModel):
    """Mini Agent 应用配置"""

    # 基础配置
    log_level: str = Field(default="INFO", description="日志级别")
    debug: bool = Field(default=False, description="调试模式")

    # 模型配置
    models: list[ModelConfig] = Field(default_factory=list, description="可用模型列表")
    default_model: str = Field(default="gpt-4", description="默认模型名称")

    # 沙箱配置
    sandbox_enabled: bool = Field(default=True, description="是否启用沙箱")
    sandbox_work_dir: str = Field(default="work", description="沙箱工作目录")

    # 记忆配置
    memory_enabled: bool = Field(default=True, description="是否启用记忆")
    memory_max_messages: int = Field(default=100, description="最大保留消息数")

    # 工具配置
    tools_enabled: bool = Field(default=True, description="是否启用工具")

    class Config:
        extra = "allow"  # 允许额外字段
        frozen = False  # 允许修改

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        """
        递归解析环境变量

        支持 $VAR_NAME 和 ${VAR_NAME} 语法
        """
        if isinstance(config, str):
            if config.startswith("$"):
                # $VAR_NAME 或 ${VAR_NAME}
                var_name = config[1:].lstrip("{").rstrip("}")
                env_value = os.getenv(var_name)
                if env_value is None:
                    raise ValueError(f"环境变量 {var_name} 未定义")
                return env_value
            return config
        elif isinstance(config, dict):
            return {k: cls.resolve_env_variables(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        return config

    @classmethod
    def from_file(cls, config_path: str | None = None) -> Self:
        """从 YAML 文件加载配置"""
        paths = Paths()

        try:
            resolved_path = paths.resolve_config_path(config_path)

            with open(resolved_path, encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}

            # 解析环境变量
            config_data = cls.resolve_env_variables(config_data)

            return cls.model_validate(config_data)

        except FileNotFoundError:
            logger.warning(f"配置文件不存在，使用默认配置")
            # 返回默认配置
            return cls._get_default_config()

    @classmethod
    def _get_default_config(cls) -> 'AppConfig':
        """获取默认配置"""
        return cls(
            log_level="INFO",
            debug=False,
            models=[],
            default_model="gpt-4",
            sandbox_enabled=True,
            sandbox_work_dir="work",
            memory_enabled=True,
            memory_max_messages=100,
            tools_enabled=True,
        )

    def get_model_config(self, name: str | None = None) -> ModelConfig:
        """获取模型配置"""
        model_name = name or self.default_model

        for model in self.models:
            if model.name == model_name:
                return model

        # 如果找不到，返回第一个
        if self.models:
            return self.models[0]

        raise ValueError(f"未找到模型配置: {model_name}")


# 全局单例和缓存
_app_config: AppConfig | None = None
_config_path: Path | None = None
_config_mtime: float | None = None


def _get_config_mtime(config_path: Path) -> float | None:
    """获取配置文件修改时间"""
    try:
        return config_path.stat().st_mtime
    except OSError:
        return None


def _load_and_cache_config(config_path: str | None = None) -> AppConfig:
    """加载配置并缓存"""
    global _app_config, _config_path, _config_mtime

    paths = Paths()
    resolved_path = paths.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _config_path = resolved_path
    _config_mtime = _get_config_mtime(resolved_path)

    logger.info(f"配置已加载: {resolved_path}")
    return _app_config


def get_app_config() -> AppConfig:
    """
    获取应用配置单例

    自动检测配置文件变化并重新加载
    """
    global _app_config, _config_path, _config_mtime

    if _app_config is None:
        return _load_and_cache_config()

    # 检查文件变化
    paths = Paths()
    resolved_path = paths.resolve_config_path()
    current_mtime = _get_config_mtime(resolved_path)

    should_reload = (
        _config_path != resolved_path or
        _config_mtime != current_mtime
    )

    if should_reload:
        logger.info("配置文件已修改，重新加载")
        return _load_and_cache_config()

    return _app_config


def reload_app_config(config_path: str | None = None) -> AppConfig:
    """强制重新加载配置"""
    return _load_and_cache_config(config_path)


def reset_app_config() -> None:
    """重置配置缓存"""
    global _app_config, _config_path, _config_mtime
    _app_config = None
    _config_path = None
    _config_mtime = None


def set_app_config(config: AppConfig) -> None:
    """设置自定义配置（用于测试）"""
    global _app_config, _config_path, _config_mtime
    _app_config = config
    _config_path = None
    _config_mtime = None
