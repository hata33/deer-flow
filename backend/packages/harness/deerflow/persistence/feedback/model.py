"""用户对运行的反馈 ORM 模型。

定义了 feedback 表的结构，存储用户对特定运行（Run）的评价信息。
每个反馈记录包含评分（点赞/点踩）、可选评论，以及指向具体消息的引用。

表设计要点:
  - 唯一约束 (thread_id, run_id, user_id)：确保每个用户对同一运行只有一条反馈
  - message_id 可选：允许反馈指向运行中的特定消息，或整个运行
  - rating 只允许 +1（点赞）或 -1（点踩），由 Repository 层校验
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class FeedbackRow(Base):
    """feedback 表的 ORM 模型。

    存储用户对 AI 运行结果的反馈数据，用于评估和改进 Agent 表现。

    字段说明:
      - feedback_id:  主键，UUID 格式的反馈唯一标识
      - run_id:       关联的运行 ID（有索引，支持按运行查询反馈）
      - thread_id:    关联的线程 ID（有索引，支持按线程查询反馈）
      - user_id:      反馈提交者的用户 ID（有索引，支持按用户过滤）
      - message_id:   可选，指向 RunEventStore 中的特定事件标识，
                      允许反馈针对某条具体消息而非整个运行
      - rating:       评分，+1（点赞）或 -1（点踩）
      - comment:      可选的文字反馈
      - created_at:   创建时间（UTC）
    """

    __tablename__ = "feedback"

    # 唯一约束：同一用户对同一线程中的同一运行只能有一条反馈记录
    # 作用：防止重复提交，同时支持 upsert 操作
    __table_args__ = (UniqueConstraint("thread_id", "run_id", "user_id", name="uq_feedback_thread_run_user"),)

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)   # 反馈唯一标识
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # 运行 ID（有索引）
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # 线程 ID（有索引）
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)  # 用户 ID（有索引，可为空表示匿名反馈）
    message_id: Mapped[str | None] = mapped_column(String(64))
    # message_id 是可选的 RunEventStore 事件标识符 ——
    # 允许反馈针对特定消息，或针对整个运行

    rating: Mapped[int] = mapped_column(nullable=False)
    # +1（点赞）或 -1（点踩），值约束在 Repository 层校验

    comment: Mapped[str | None] = mapped_column(Text)
    # 可选的文字反馈，用户可以附加详细说明

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # 反馈创建时间，默认为当前 UTC 时间
