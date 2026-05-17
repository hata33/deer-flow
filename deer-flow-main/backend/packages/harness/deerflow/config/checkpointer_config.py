"""LangGraph 状态持久化（Checkpointer）配置。

本模块定义了 LangGraph 的 checkpoint 存储后端配置。
Checkpointer 用于持久化 LangGraph 图的执行状态，使得对话可以跨请求恢复。

支持的后端类型：
    - **memory** — 进程内存存储（默认）。
      重启后数据丢失，仅适用于开发测试。
    - **sqlite** — SQLite 文件数据库。
      持久化到本地文件，需要安装 ``langgraph-checkpoint-sqlite``。
      connection_string 为文件路径（如 ``.deer-flow/checkpoints.db``）
      或 ``:memory:`` 表示内存中的 SQLite。
    - **postgres** — PostgreSQL 数据库。
      适合生产环境，需要安装 ``langgraph-checkpoint-postgres``。
      connection_string 为 PostgreSQL DSN
      （如 ``postgresql://user:pass@localhost:5432/db``）。

配置示例（config.yaml）：
    ```yaml
    checkpointer:
      type: sqlite
      connection_string: .deer-flow/checkpoints.db
    ```

全局实例管理：
    - None 表示未配置 checkpointer（使用 LangGraph 默认行为）。
    - 通过 load_checkpointer_config_from_dict() 从 config.yaml 加载。
"""
from typing import Literal

from pydantic import BaseModel, Field

# 支持的 checkpointer 后端类型
CheckpointerType = Literal["memory", "sqlite", "postgres"]


class CheckpointerConfig(BaseModel):
    """LangGraph 状态持久化 checkpointer 配置。

    Attributes:
        type: 后端类型。
            - 'memory': 进程内存存储（重启后丢失）
            - 'sqlite': 本地 SQLite 文件（需安装 langgraph-checkpoint-sqlite）
            - 'postgres': PostgreSQL 数据库（需安装 langgraph-checkpoint-postgres）
        connection_string: 连接字符串。
            - sqlite: 文件路径（如 '.deer-flow/checkpoints.db'）或 ':memory:'
            - postgres: DSN（如 'postgresql://user:pass@localhost:5432/db'）
            - memory 类型不需要此字段
    """

    type: CheckpointerType = Field(
        description="Checkpointer 后端类型。"
        "'memory' 是进程内存存储（重启后丢失）。"
        "'sqlite' 持久化到本地文件（需要 langgraph-checkpoint-sqlite）。"
        "'postgres' 持久化到 PostgreSQL（需要 langgraph-checkpoint-postgres）。",
    )
    connection_string: str | None = Field(
        default=None,
        description="sqlite 连接字符串（文件路径）或 postgres DSN。"
        "sqlite 和 postgres 类型必需。"
        "sqlite 使用文件路径如 '.deer-flow/checkpoints.db' 或 ':memory:'。"
        "postgres 使用 DSN 如 'postgresql://user:pass@localhost:5432/db'。",
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
# None 表示未配置 checkpointer
_checkpointer_config: CheckpointerConfig | None = None


def get_checkpointer_config() -> CheckpointerConfig | None:
    """获取当前 checkpointer 配置。未配置时返回 None。"""
    return _checkpointer_config


def set_checkpointer_config(config: CheckpointerConfig | None) -> None:
    """直接设置 checkpointer 配置。"""
    global _checkpointer_config
    _checkpointer_config = config


def load_checkpointer_config_from_dict(config_dict: dict) -> None:
    """从字典加载 checkpointer 配置（由 AppConfig.from_file 调用）。"""
    global _checkpointer_config
    _checkpointer_config = CheckpointerConfig(**config_dict)
