"""运行（Run）元数据 ORM 模型。

定义了 runs 表的结构，存储每次 Agent 运行的汇总信息。
与 run_events 表（存储事件流）不同，本表存储运行级别的汇总数据，
包括状态、模型、Token 用量等。

表设计要点:
  - run_id 为主键：每次运行有唯一 ID
  - thread_id 有索引：支持按线程查询运行列表
  - status 有索引（复合索引）：支持按状态过滤
  - 便利字段（message_count, first_human_message, last_ai_message）：
    避免列表页查询时需要访问 run_events 表
  - Token 用量字段：按调用方类型分类统计，支持成本分析
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


class RunRow(Base):
    """runs 表的 ORM 模型。

    存储每次 Agent 运行的元数据，用于运行列表、状态跟踪和 Token 用量统计。

    字段分组:
      1. 基本信息：run_id, thread_id, assistant_id, user_id, status
      2. 运行参数：model_name, multitask_strategy, metadata_json, kwargs_json
      3. 便利字段：message_count, first_human_message, last_ai_message
      4. Token 统计：total_input_tokens, total_output_tokens 等
      5. 关联信息：follow_up_to_run_id
      6. 时间戳：created_at, updated_at
    """

    __tablename__ = "runs"

    # ---- 基本信息 ----
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)       # 运行唯一标识
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # 所属线程（有索引）
    assistant_id: Mapped[str | None] = mapped_column(String(128))           # 关联的助手 ID
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)     # 所有者用户 ID（有索引）
    status: Mapped[str] = mapped_column(String(20), default="pending")      # 运行状态
    # 状态值: "pending" | "running" | "success" | "error" | "timeout" | "interrupted"

    # ---- 运行参数 ----
    model_name: Mapped[str | None] = mapped_column(String(128))             # 使用的模型名称
    multitask_strategy: Mapped[str] = mapped_column(String(20), default="reject")  # 多任务策略
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)         # 运行元数据（JSON）
    kwargs_json: Mapped[dict] = mapped_column(JSON, default=dict)           # 运行参数（JSON）
    error: Mapped[str | None] = mapped_column(Text)                         # 错误信息（运行失败时）

    # ---- 便利字段 ----
    # 冗余存储在 runs 表中，避免列表页需要 JOIN run_events 表。
    # 这些字段在运行完成时由 RunJournal 写入。
    message_count: Mapped[int] = mapped_column(default=0)                   # 消息总数
    first_human_message: Mapped[str | None] = mapped_column(Text)           # 第一条用户消息（摘要）
    last_ai_message: Mapped[str | None] = mapped_column(Text)               # 最后一条 AI 消息（摘要）

    # ---- Token 用量统计 ----
    # 由 RunJournal 在内存中累积，运行完成时写入数据库。
    # 按调用方类型分类统计，支持成本分析和优化决策。
    total_input_tokens: Mapped[int] = mapped_column(default=0)              # 输入 Token 总数
    total_output_tokens: Mapped[int] = mapped_column(default=0)             # 输出 Token 总数
    total_tokens: Mapped[int] = mapped_column(default=0)                    # Token 总数
    llm_call_count: Mapped[int] = mapped_column(default=0)                  # LLM 调用次数
    lead_agent_tokens: Mapped[int] = mapped_column(default=0)               # 主 Agent 消耗 Token
    subagent_tokens: Mapped[int] = mapped_column(default=0)                 # 子 Agent 消耗 Token
    middleware_tokens: Mapped[int] = mapped_column(default=0)               # 中间件消耗 Token

    # ---- 关联信息 ----
    # 记录本次运行是哪次运行的后续追问，形成运行链
    follow_up_to_run_id: Mapped[str | None] = mapped_column(String(64))

    # ---- 时间戳 ----
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),        # 创建时设置默认值
        onupdate=lambda: datetime.now(UTC),       # 每次更新时自动刷新
    )

    __table_args__ = (
        # 复合索引：优化按线程+状态查询（如获取线程中所有正在运行的运行）
        Index("ix_runs_thread_status", "thread_id", "status"),
    )
