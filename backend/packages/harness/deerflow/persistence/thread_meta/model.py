"""线程元数据 ORM 模型。

定义了 threads_meta 表的结构，存储线程的元数据信息。
每个线程对应一条记录，包括显示名称、状态、所有者和自定义元数据。

表设计要点:
  - thread_id 为主键：每个线程有唯一 ID
  - user_id 有索引：支持按用户查询线程列表
  - metadata_json 使用 JSON 类型：存储灵活的自定义元数据
  - updated_at 自动更新：通过 onupdate 回调自动维护更新时间
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class ThreadMetaRow(Base):
    """threads_meta 表的 ORM 模型。

    存储线程的元数据，用于线程列表展示、权限控制和状态管理。

    字段说明:
      - thread_id:     线程唯一标识（主键）
      - assistant_id:  关联的助手 ID（有索引，支持按助手过滤）
      - user_id:       所有者用户 ID（有索引，支持按用户过滤）
      - display_name:  显示名称/标题（如 "帮我写一篇关于AI的文章"）
      - status:        线程状态（如 "idle" 空闲、"active" 活跃）
      - metadata_json: 自定义元数据（JSON 格式，用于搜索过滤等）
      - created_at:    创建时间
      - updated_at:    更新时间（自动维护）
    """

    __tablename__ = "threads_meta"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)       # 线程唯一标识
    assistant_id: Mapped[str | None] = mapped_column(String(128), index=True)  # 助手 ID（有索引）
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)        # 所有者（有索引）
    display_name: Mapped[str | None] = mapped_column(String(256))              # 显示名称/标题
    status: Mapped[str] = mapped_column(String(20), default="idle")            # 线程状态
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)            # 自定义元数据
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),        # 创建时设置
        onupdate=lambda: datetime.now(UTC),       # 每次更新自动刷新
    )
