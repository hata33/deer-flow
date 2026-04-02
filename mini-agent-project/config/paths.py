"""
路径配置

定义系统中使用的各种路径。
"""
import os
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field


class Paths(BaseModel):
    """系统路径配置"""

    # 基础目录
    base_dir: Path = Field(default_factory=lambda: Path.cwd())
    work_dir: Path = Field(default_factory=lambda: Path.cwd() / "work")
    data_dir: Path = Field(default_factory=lambda: Path.cwd() / "data")

    # 线程数据目录
    threads_dir: Path = Field(default_factory=lambda: Path.cwd() / "data" / "threads")

    # 配置文件路径
    config_path: Path = Field(default_factory=lambda: Path.cwd() / "config.yaml")

    def model_post_init(self, __context: object) -> None:
        """确保目录存在"""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.threads_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path:
        """
        解析配置文件路径

        优先级：
        1. 提供的 config_path 参数
        2. MINI_AGENT_CONFIG_PATH 环境变量
        3. 当前目录的 config.yaml
        4. 父目录的 config.yaml
        """
        if config_path:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"配置文件不存在: {path}")
            return path

        env_path = os.getenv("MINI_AGENT_CONFIG_PATH")
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise FileNotFoundError(f"环境变量指定的配置文件不存在: {path}")
            return path

        # 检查当前目录
        path = Path.cwd() / "config.yaml"
        if path.exists():
            return path

        # 检查父目录
        path = Path.cwd().parent / "config.yaml"
        if path.exists():
            return path

        raise FileNotFoundError("找不到 config.yaml 文件")


# 全局单例
_paths: Paths | None = None


def get_paths() -> Paths:
    """获取路径配置单例"""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths


def reset_paths() -> None:
    """重置路径配置"""
    global _paths
    _paths = None
