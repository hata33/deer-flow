"""运行时路径解析 — 项目根目录与状态目录定位。

本模块提供 DeerFlow 运行时的基础路径解析能力，是整个配置系统的最底层依赖。
所有需要定位文件系统路径的模块（config、paths、skills 等）最终都依赖这里的函数。

解析优先级（从高到低）：
1. 环境变量覆盖（如 DEER_FLOW_PROJECT_ROOT、DEER_FLOW_HOME）
2. 项目根目录下的约定路径（如 .deer-flow、skills/）
"""

import os
from pathlib import Path


def project_root() -> Path:
    """返回调用方的项目根目录，用于定位运行时拥有的文件。

    解析优先级：
    1. DEER_FLOW_PROJECT_ROOT 环境变量（必须指向已存在的目录）
    2. 当前工作目录（CWD）

    为什么用 CWD 而不是 __file__ 推导：
    - DeerFlow 作为可发布包（harness），可能被安装到任意位置
    - 调用方的 CWD 才是项目实际运行的根目录
    - 环境变量提供显式覆盖机制
    """
    if env_root := os.getenv("DEER_FLOW_PROJECT_ROOT"):
        root = Path(env_root).resolve()
        if not root.exists():
            raise ValueError(f"DEER_FLOW_PROJECT_ROOT is set to '{env_root}', but the resolved path '{root}' does not exist.")
        if not root.is_dir():
            raise ValueError(f"DEER_FLOW_PROJECT_ROOT is set to '{env_root}', but the resolved path '{root}' is not a directory.")
        return root
    return Path.cwd().resolve()


def runtime_home() -> Path:
    """返回可写的 DeerFlow 状态目录。

    解析优先级：
    1. DEER_FLOW_HOME 环境变量
    2. {project_root}/.deer-flow

    状态目录用于存储所有运行时数据：线程目录、用户数据、记忆文件、自定义代理等。
    """
    if env_home := os.getenv("DEER_FLOW_HOME"):
        return Path(env_home).resolve()
    return project_root() / ".deer-flow"


def resolve_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """解析路径为绝对路径。

    - 绝对路径：原样返回（规范化后）
    - 相对路径：基于项目根目录（或指定的 base）解析为绝对路径

    此函数用于配置文件中的相对路径解析，确保无论 CWD 在哪里，
    配置中指定的路径都能正确指向预期位置。
    """
    path = Path(value)
    if not path.is_absolute():
        path = (base or project_root()) / path
    return path.resolve()


def existing_project_file(names: tuple[str, ...]) -> Path | None:
    """在项目根目录下查找第一个存在的指定文件。

    参数是文件名元组（不是路径），只搜索项目根目录一级。
    用于定位 config.yaml、extensions_config.json 等配置文件。

    Returns:
        找到的文件路径，或 None（文件不存在时不报错）。
    """
    root = project_root()
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None
