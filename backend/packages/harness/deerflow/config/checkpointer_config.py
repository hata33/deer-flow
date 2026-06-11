"""LangGraph Checkpointer 配置 — 对话状态持久化。

Checkpointer 负责保存 LangGraph Agent 的对话状态（消息、工具调用、中间结果），
使对话能够跨请求持久化和恢复。

注意：此配置独立于 DatabaseConfig。
当用户配置了 DatabaseConfig 但未显式配置 Checkpointer 时，
系统会根据 DatabaseConfig.backend 自动推断 Checkpointer 类型（见 runtime/checkpointer.py）。

当用户显式配置了此模块（config.yaml 中的 checkpointer 字段），
则以此配置为准，覆盖自动推断。

### 后端类型
- memory: 进程内存储，重启后丢失。仅用于开发。
- sqlite: 本地文件持久化。需要 langgraph-checkpoint-sqlite。
- postgres: PostgreSQL 持久化。需要 deerflow-harness[postgres]。
"""

from typing import Literal

from pydantic import BaseModel, Field

CheckpointerType = Literal["memory", "sqlite", "postgres"]


class CheckpointerConfig(BaseModel):
    """LangGraph 状态持久化 Checkpointer 配置。

    - type: 后端类型
    - connection_string: 连接字符串
      - sqlite: 文件路径（如 .deer-flow/checkpoints.db 或 :memory:）
      - postgres: DSN（如 postgresql://user:pass@localhost:5432/db）
      - sqlite 可省略（默认 store.db）
      - postgres 必需
    """

    type: CheckpointerType = Field(
        description="Checkpointer backend type. "
        "'memory' is in-process only (lost on restart). "
        "'sqlite' persists to a local file (requires langgraph-checkpoint-sqlite). "
        "'postgres' persists to PostgreSQL (install with deerflow-harness[postgres])."
    )
    connection_string: str | None = Field(
        default=None,
        description="Connection string for sqlite (file path) or postgres (DSN). "
        "Optional for sqlite and defaults to 'store.db' when omitted. "
        "Required for postgres. "
        "For sqlite, use a file path like '.deer-flow/checkpoints.db' or ':memory:' for in-memory. "
        "For postgres, use a DSN like 'postgresql://user:pass@localhost:5432/db'.",
    )


# 全局单例 — None 表示未显式配置，由运行时根据 DatabaseConfig 自动推断
_checkpointer_config: CheckpointerConfig | None = None


def get_checkpointer_config() -> CheckpointerConfig | None:
    """获取当前 Checkpointer 配置。

    返回 None 表示未显式配置，调用方应根据 DatabaseConfig 推断。
    """
    return _checkpointer_config


def set_checkpointer_config(config: CheckpointerConfig | None) -> None:
    """设置 Checkpointer 配置。"""
    global _checkpointer_config
    _checkpointer_config = config


def ensure_config_loaded() -> None:
    """Lazily load app config when checkpointer config has not been initialized."""
    from deerflow.config.app_config import _app_config, get_app_config

    config = get_checkpointer_config()
    if config is not None or _app_config is not None:
        return

    try:
        get_app_config()
    except FileNotFoundError:
        pass


def load_checkpointer_config_from_dict(config_dict: dict | None) -> None:
    """从字典加载 Checkpointer 配置（由 AppConfig 初始化时调用）。

    config_dict 为 None 时清除配置（回到自动推断模式）。
    """
    global _checkpointer_config
    if config_dict is None:
        _checkpointer_config = None
        return
    _checkpointer_config = CheckpointerConfig(**config_dict)
