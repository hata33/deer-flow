"""运行事件（RunEvent）ORM 模型。

定义了 run_events 表的结构，存储运行过程中的各类事件记录。
事件按类型分为:
  - message:   对话消息（用户输入、AI 回复等）
  - trace:     追踪信息（工具调用、中间步骤等）
  - lifecycle: 生命周期事件（开始、完成、取消等）

表设计要点:
  - 自增整数主键（id）：事件量大，使用整数主键提升插入和索引性能
  - 唯一约束 (thread_id, seq)：确保同一线程内事件序号唯一
  - 复合索引：优化按线程+分类+序号、按线程+运行+序号的查询
  - user_id 可空：兼容认证功能引入前创建的历史数据
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunEventRow(Base):
    """run_events 表的 ORM 模型。

    存储运行过程中的所有事件，用于消息回放、历史查看和调试分析。

    字段说明:
      - id:             自增主键
      - thread_id:      所属线程 ID
      - run_id:         所属运行 ID
      - user_id:        对话所有者 ID（可空，兼容历史数据）
      - event_type:     事件类型标识
      - category:       事件分类: "message" | "trace" | "lifecycle"
      - content:        事件内容文本
      - event_metadata: 事件元数据（JSON 格式，存储额外信息）
      - seq:            事件序号（同一线程内递增）
      - created_at:     创建时间
    """

    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)  # 自增主键
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)     # 线程 ID
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)        # 运行 ID
    # 对话所有者的用户 ID。可为空以兼容认证功能引入前创建的数据；
    # 新写入由认证中间件自动填充，启动时的孤儿迁移脚本会补充历史数据。
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)    # 事件类型
    category: Mapped[str] = mapped_column(String(16), nullable=False)      # 事件分类
    # "message" | "trace" | "lifecycle"
    content: Mapped[str] = mapped_column(Text, default="")                  # 事件内容
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)        # 元数据（JSON）
    seq: Mapped[int] = mapped_column(nullable=False)                        # 事件序号
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    __table_args__ = (
        # 同一线程内事件序号唯一，防止重复写入
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        # 优化按线程+分类+序号查询（如获取某线程所有消息）
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        # 优化按线程+运行+序号查询（如获取某次运行的所有事件）
        Index("ix_events_run", "thread_id", "run_id", "seq"),
    )
