"""SQLAlchemy 用户仓库实现 — 基于共享异步会话工厂。

本模块是 UserRepository 抽象接口的 SQLAlchemy 实现，使用
deerflow.persistence.engine 提供的共享异步会话工厂。

核心设计：
  - users 表与其他业务表（threads_meta、runs、run_events、feedback）
    共享同一个数据库和会话工厂
  - 构造函数直接接收会话工厂（与其他四个仓库一致的模式）
  - 调用者必须在 init_engine_from_config() 之后才能构造本类

数据转换：
  - _row_to_user：SQLAlchemy UserRow → Pydantic User
    处理 SQLite 时区信息丢失问题（重新附加 UTC）
  - _user_to_row：Pydantic User → SQLAlchemy UserRow

关键实现细节：
  - create_user 使用 IntegrityError 检测邮箱重复
  - update_user 硬失败策略：行不存在时抛 UserNotFoundError
  - 所有写操作在独立会话中执行并提交
  - UTC 时区处理：SQLite 读取时丢失 tzinfo，_row_to_user 自动补充

错误处理：
  - 邮箱重复 → ValueError（通过 IntegrityError 检测）
  - 并发删除 → UserNotFoundError（update_user 中显式检查）
  - 所有异常向上传播，由调用者决定处理策略
"""

from __future__ import annotations

from datetime import UTC
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gateway.auth.models import User
from app.gateway.auth.repositories.base import UserNotFoundError, UserRepository
from deerflow.persistence.user.model import UserRow


class SQLiteUserRepository(UserRepository):
    """基于共享 SQLAlchemy 引擎的异步用户仓库。

    使用 deerflow.persistence.engine 提供的共享异步会话工厂
    访问用户数据。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ── 数据转换器 ────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: UserRow) -> User:
        """将 SQLAlchemy UserRow 转换为 Pydantic User。

        处理 SQLite 时区信息丢失问题：读取的时间戳可能没有 tzinfo，
        此处重新附加 UTC 时区以确保下游代码可以可靠地比较时间戳。

        Args:
            row: SQLAlchemy UserRow 实例。

        Returns:
            Pydantic User 实例。
        """
        return User(
            id=UUID(row.id),
            email=row.email,
            password_hash=row.password_hash,
            system_role=row.system_role,  # type: ignore[arg-type]
            # SQLite 读取时丢失 tzinfo；重新附加 UTC 确保下游代码可以可靠比较时间戳
            created_at=row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC),
            oauth_provider=row.oauth_provider,
            oauth_id=row.oauth_id,
            needs_setup=row.needs_setup,
            token_version=row.token_version,
        )

    @staticmethod
    def _user_to_row(user: User) -> UserRow:
        """将 Pydantic User 转换为 SQLAlchemy UserRow。

        Args:
            user: Pydantic User 实例。

        Returns:
            SQLAlchemy UserRow 实例。
        """
        return UserRow(
            id=str(user.id),
            email=user.email,
            password_hash=user.password_hash,
            system_role=user.system_role,
            created_at=user.created_at,
            oauth_provider=user.oauth_provider,
            oauth_id=user.oauth_id,
            needs_setup=user.needs_setup,
            token_version=user.token_version,
        )

    # ── CRUD 操作 ──────────────────────────────────────────────────────

    async def create_user(self, user: User) -> User:
        """插入新用户。邮箱重复时抛 ValueError。

        Args:
            user: 要创建的 User 对象。

        Returns:
            创建的 User。

        Raises:
            ValueError: 邮箱已被注册。
        """
        row = self._user_to_row(user)
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(f"Email already registered: {user.email}") from exc
        return user

    async def get_user_by_id(self, user_id: str) -> User | None:
        """按 ID 获取用户。

        Args:
            user_id: 用户 UUID 字符串。

        Returns:
            找到时返回 User，否则返回 None。
        """
        async with self._sf() as session:
            row = await session.get(UserRow, user_id)
            return self._row_to_user(row) if row is not None else None

    async def get_user_by_email(self, email: str) -> User | None:
        """按邮箱获取用户。

        Args:
            email: 用户邮箱地址。

        Returns:
            找到时返回 User，否则返回 None。
        """
        stmt = select(UserRow).where(UserRow.email == email)
        async with self._sf() as session:
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._row_to_user(row) if row is not None else None

    async def update_user(self, user: User) -> User:
        """更新已有用户。

        硬失败策略：如果 user.id 对应的行不存在，抛出 UserNotFoundError
        而非静默成功。调用者（reset_admin、密码修改处理函数、
        _ensure_admin_user）在此调用之前都已获取用户，
        因此行缺失意味着在操作期间行被并发删除。静默成功会让调用者
        为不存在的行记录"密码已重置"。

        Args:
            user: 包含更新字段的 User 对象。

        Returns:
            更新后的 User。

        Raises:
            UserNotFoundError: 用户行不存在。
        """
        async with self._sf() as session:
            row = await session.get(UserRow, str(user.id))
            if row is None:
                raise UserNotFoundError(f"User {user.id} no longer exists")
            row.email = user.email
            row.password_hash = user.password_hash
            row.system_role = user.system_role
            row.oauth_provider = user.oauth_provider
            row.oauth_id = user.oauth_id
            row.needs_setup = user.needs_setup
            row.token_version = user.token_version
            await session.commit()
        return user

    async def count_users(self) -> int:
        """返回注册用户总数。"""
        stmt = select(func.count()).select_from(UserRow)
        async with self._sf() as session:
            return await session.scalar(stmt) or 0

    async def count_admin_users(self) -> int:
        """返回 system_role == 'admin' 的用户数量。"""
        stmt = select(func.count()).select_from(UserRow).where(UserRow.system_role == "admin")
        async with self._sf() as session:
            return await session.scalar(stmt) or 0

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> User | None:
        """按 OAuth 提供者和 ID 获取用户。

        Args:
            provider: OAuth 提供者名称（如 'github'）。
            oauth_id: OAuth 提供者中的用户 ID。

        Returns:
            找到时返回 User，否则返回 None。
        """
        stmt = select(UserRow).where(UserRow.oauth_provider == provider, UserRow.oauth_id == oauth_id)
        async with self._sf() as session:
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._row_to_user(row) if row is not None else None
