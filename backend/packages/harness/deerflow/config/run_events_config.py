"""运行事件存储配置 — 对话消息与执行追踪的持久化。

运行事件（Run Events）记录 Agent 每次运行的完整信息：
- 对话消息（用户输入、AI 回复、工具调用和结果）
- 执行追踪（中间步骤、性能数据）

### 后端类型
- memory: 内存存储。重启后数据丢失。适合开发和测试。
- db: SQLAlchemy ORM 数据库存储。提供完整查询能力。适合生产部署。
- jsonl: 追加式 JSONL 文件。轻量级替代方案，适合单节点需要持久化但不需要数据库的场景。

本配置是 AppConfig 的直接字段，不需要全局单例。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RunEventsConfig(BaseModel):
    """运行事件存储配置。

    - backend: 存储后端类型
    - max_trace_content: 追踪内容的最大字节数（仅 db 后端生效，超出截断）
    - track_token_usage: 是否由 RunJournal 累积 token 使用量到 RunRow
    """

    backend: Literal["memory", "db", "jsonl"] = Field(
        default="memory",
        description="Storage backend for run events. 'memory' for development (no persistence), 'db' for production (SQL queries), 'jsonl' for lightweight single-node persistence.",
    )
    max_trace_content: int = Field(
        default=10240,
        description="Maximum trace content size in bytes before truncation (db backend only).",
    )
    track_token_usage: bool = Field(
        default=True,
        description="Whether RunJournal should accumulate token counts to RunRow.",
    )
