"""基于 SQLAlchemy 的反馈（Feedback）数据仓库。

每个方法获取自己的短生命周期会话（session），操作完成后立即释放。
这样设计的原因是：反馈操作是独立的读写请求，不需要跨方法共享事务，
同时避免在异步环境中长时间持有数据库连接。

所有修改和查询方法都接受 user_id 参数，具有三态语义:
  - AUTO（默认）: 从请求作用域的 contextvar 自动解析用户 ID
  - 显式 str:    使用提供的值
  - 显式 None:   绕过所有者过滤（仅用于迁移/CLI 场景）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id


class FeedbackRepository:
    """反馈数据仓库，封装所有反馈相关的数据库操作。

    通过 async_sessionfactory 创建短生命周期会话，确保每个方法
    在独立的事务中执行，避免长事务持有连接。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 保存会话工厂，各方法通过它创建独立会话
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: FeedbackRow) -> dict:
        """将 ORM 行转换为字典，并处理日期时间格式。

        将 datetime 对象转为 ISO 格式字符串，确保与 JSON 序列化兼容。
        """
        d = row.to_dict()
        val = d.get("created_at")
        if isinstance(val, datetime):
            d["created_at"] = val.isoformat()
        return d

    async def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        rating: int,
        user_id: str | None | _AutoSentinel = AUTO,
        message_id: str | None = None,
        comment: str | None = None,
    ) -> dict:
        """创建一条反馈记录。rating 必须为 +1 或 -1。

        流程:
          1. 校验评分值
          2. 解析用户 ID（支持 AUTO/显式值/None 三态）
          3. 创建 ORM 对象并写入数据库
          4. 刷新对象以获取数据库生成的字段值
          5. 返回字典格式的反馈数据
        """
        if rating not in (1, -1):
            raise ValueError(f"rating must be +1 or -1, got {rating}")
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.create")
        row = FeedbackRow(
            feedback_id=str(uuid.uuid4()),  # 生成唯一 ID
            run_id=run_id,
            thread_id=thread_id,
            user_id=resolved_user_id,
            message_id=message_id,
            rating=rating,
            comment=comment,
            created_at=datetime.now(UTC),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()       # 提交事务
            await session.refresh(row)   # 刷新以获取数据库默认值
            return self._row_to_dict(row)

    async def get(
        self,
        feedback_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> dict | None:
        """根据 ID 获取单条反馈记录。

        包含所有者过滤：如果解析到用户 ID，则只返回属于该用户的反馈。
        返回 None 表示记录不存在或不属于当前用户。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.get")
        async with self._sf() as session:
            row = await session.get(FeedbackRow, feedback_id)
            if row is None:
                return None
            # 所有者过滤：非 None 时只返回匹配用户的记录
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return None
            return self._row_to_dict(row)

    async def list_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        limit: int = 100,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict]:
        """列出指定线程中某个运行的所有反馈。

        按创建时间升序排列（最早的在前）。
        支持所有者过滤和分页限制。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.list_by_run")
        stmt = select(FeedbackRow).where(FeedbackRow.thread_id == thread_id, FeedbackRow.run_id == run_id)
        if resolved_user_id is not None:
            stmt = stmt.where(FeedbackRow.user_id == resolved_user_id)
        stmt = stmt.order_by(FeedbackRow.created_at.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_by_thread(
        self,
        thread_id: str,
        *,
        limit: int = 100,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict]:
        """列出指定线程中的所有反馈。

        按创建时间升序排列。支持所有者过滤和分页限制。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.list_by_thread")
        stmt = select(FeedbackRow).where(FeedbackRow.thread_id == thread_id)
        if resolved_user_id is not None:
            stmt = stmt.where(FeedbackRow.user_id == resolved_user_id)
        stmt = stmt.order_by(FeedbackRow.created_at.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def delete(
        self,
        feedback_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> bool:
        """删除一条反馈记录。

        返回 True 表示成功删除，False 表示记录不存在或不属于当前用户。
        包含所有者过滤以防止用户删除他人的反馈。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.delete")
        async with self._sf() as session:
            row = await session.get(FeedbackRow, feedback_id)
            if row is None:
                return False
            # 所有者检查：非 None 时只允许删除自己的反馈
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def upsert(
        self,
        *,
        run_id: str,
        thread_id: str,
        rating: int,
        user_id: str | None | _AutoSentinel = AUTO,
        comment: str | None = None,
    ) -> dict:
        """创建或更新反馈（upsert 语义）。

        查找 (thread_id, run_id, user_id) 组合的已有反馈记录:
          - 如果存在：更新评分和评论，重置创建时间
          - 如果不存在：创建新记录

        这种设计使用户可以方便地修改自己的反馈，无需先删后建。
        """
        if rating not in (1, -1):
            raise ValueError(f"rating must be +1 or -1, got {rating}")
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.upsert")
        async with self._sf() as session:
            # 查找已有记录
            stmt = select(FeedbackRow).where(
                FeedbackRow.thread_id == thread_id,
                FeedbackRow.run_id == run_id,
                FeedbackRow.user_id == resolved_user_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                # 更新已有记录
                row.rating = rating
                row.comment = comment
                row.created_at = datetime.now(UTC)  # 重置时间戳
            else:
                # 创建新记录
                row = FeedbackRow(
                    feedback_id=str(uuid.uuid4()),
                    run_id=run_id,
                    thread_id=thread_id,
                    user_id=resolved_user_id,
                    rating=rating,
                    comment=comment,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def delete_by_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> bool:
        """删除当前用户对某个运行的反馈。

        返回 True 表示成功删除，False 表示没有找到记录。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.delete_by_run")
        async with self._sf() as session:
            stmt = select(FeedbackRow).where(
                FeedbackRow.thread_id == thread_id,
                FeedbackRow.run_id == run_id,
                FeedbackRow.user_id == resolved_user_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def list_by_thread_grouped(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> dict[str, dict]:
        """按运行 ID 分组返回线程中的反馈数据。

        返回格式: {run_id: feedback_dict, ...}
        用于一次性获取线程中所有运行的反馈状态，避免逐个查询。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="FeedbackRepository.list_by_thread_grouped")
        stmt = select(FeedbackRow).where(FeedbackRow.thread_id == thread_id)
        if resolved_user_id is not None:
            stmt = stmt.where(FeedbackRow.user_id == resolved_user_id)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return {row.run_id: self._row_to_dict(row) for row in result.scalars()}

    async def aggregate_by_run(self, thread_id: str, run_id: str) -> dict:
        """使用数据库端聚合查询统计某个运行的反馈。

        通过 SQL 的 COUNT + SUM + CASE 聚合，在数据库端完成统计计算，
        避免将所有反馈记录加载到应用层再计数。

        返回:
          {
            "run_id": "xxx",
            "total": 10,       # 总反馈数
            "positive": 7,     # 点赞数
            "negative": 3,     # 点踩数
          }
        """
        stmt = select(
            func.count().label("total"),  # 总数
            func.coalesce(func.sum(case((FeedbackRow.rating == 1, 1), else_=0)), 0).label("positive"),  # 点赞数
            func.coalesce(func.sum(case((FeedbackRow.rating == -1, 1), else_=0)), 0).label("negative"),  # 点踩数
        ).where(FeedbackRow.thread_id == thread_id, FeedbackRow.run_id == run_id)
        async with self._sf() as session:
            row = (await session.execute(stmt)).one()
            return {
                "run_id": run_id,
                "total": row.total,
                "positive": row.positive,
                "negative": row.negative,
            }
