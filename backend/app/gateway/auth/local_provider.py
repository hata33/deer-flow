"""本地邮箱/密码认证提供者 — DeerFlow 默认认证实现。

本模块实现了基于本地数据库的邮箱/密码认证，是 AuthProvider
抽象基类的具体实现。

主要功能：
  1. 用户认证（authenticate）：
     - 通过邮箱查找用户
     - 验证密码哈希
     - 自动检测并升级旧版密码哈希（v1 → v2）

  2. 用户管理：
     - 创建新用户（密码自动哈希）
     - 查询用户（按 ID、邮箱、OAuth）
     - 更新用户信息
     - 统计用户数量（总数、管理员数）

核心设计：
  - 依赖注入：通过构造函数接收 UserRepository，支持不同存储后端
  - 机会性哈希升级：登录时检测旧版哈希并在后台升级，升级失败不阻塞登录
  - OAuth 用户无密码：password_hash 为 None 的用户（纯 OAuth 用户）无法通过密码登录
  - 所有异步操作通过 UserRepository 委托，保持提供者逻辑与存储解耦

错误处理策略：
  - 认证失败返回 None（而非抛异常），调用者通过 None 判断失败
  - 哈希升级失败仅记录警告，不阻塞正常登录流程
"""

import logging

from app.gateway.auth.models import User
from app.gateway.auth.password import hash_password_async, needs_rehash, verify_password_async
from app.gateway.auth.providers import AuthProvider
from app.gateway.auth.repositories.base import UserRepository

logger = logging.getLogger(__name__)


class LocalAuthProvider(AuthProvider):
    """基于本地数据库的邮箱/密码认证提供者。

    通过 UserRepository 访问用户数据，支持密码认证和用户管理。
    """

    def __init__(self, repository: UserRepository):
        """使用 UserRepository 初始化。

        Args:
            repository: UserRepository 实现（如 SQLiteUserRepository）。
        """
        self._repo = repository

    async def authenticate(self, credentials: dict) -> User | None:
        """使用邮箱和密码进行认证。

        认证流程：
          1. 提取邮箱和密码
          2. 按邮箱查找用户
          3. 验证密码哈希
          4. 检测并升级旧版哈希（机会性）

        Args:
            credentials: 包含 'email' 和 'password' 键的字典。

        Returns:
            认证成功时返回 User，失败时返回 None。
        """
        email = credentials.get("email")
        password = credentials.get("password")

        if not email or not password:
            return None

        user = await self._repo.get_user_by_email(email)
        if user is None:
            return None

        # OAuth 用户没有本地密码
        if user.password_hash is None:
            return None

        if not await verify_password_async(password, user.password_hash):
            return None

        # 机会性哈希升级：登录成功后检查是否需要升级到新版哈希格式
        if needs_rehash(user.password_hash):
            try:
                user.password_hash = await hash_password_async(password)
                await self._repo.update_user(user)
            except Exception:
                # 哈希升级是机会性操作；瞬态数据库错误不应阻止有效的登录
                logger.warning("Failed to rehash password for user %s; login will still succeed", user.email, exc_info=True)

        return user

    async def get_user(self, user_id: str) -> User | None:
        """按 ID 获取用户。

        Args:
            user_id: 用户 UUID 字符串。

        Returns:
            User 对象，或 None（未找到）。
        """
        return await self._repo.get_user_by_id(user_id)

    async def create_user(self, email: str, password: str | None = None, system_role: str = "user", needs_setup: bool = False) -> User:
        """创建新的本地用户。

        Args:
            email: 用户邮箱地址。
            password: 明文密码（将被哈希）。
            system_role: 角色分配（"admin" 或 "user"）。
            needs_setup: 为 True 时，用户首次登录需完成设置。

        Returns:
            创建的 User 实例。
        """
        password_hash = await hash_password_async(password) if password else None
        user = User(
            email=email,
            password_hash=password_hash,
            system_role=system_role,
            needs_setup=needs_setup,
        )
        return await self._repo.create_user(user)

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> User | None:
        """按 OAuth 提供者和 ID 获取用户。

        Args:
            provider: OAuth 提供者名称（如 'github'、'google'）。
            oauth_id: OAuth 提供者中的用户 ID。

        Returns:
            User 对象，或 None（未找到）。
        """
        return await self._repo.get_user_by_oauth(provider, oauth_id)

    async def count_users(self) -> int:
        """返回注册用户总数。"""
        return await self._repo.count_users()

    async def count_admin_users(self) -> int:
        """返回管理员用户数量。"""
        return await self._repo.count_admin_users()

    async def update_user(self, user: User) -> User:
        """更新已有用户。

        Args:
            user: 包含更新字段的 User 对象。

        Returns:
            更新后的 User。
        """
        return await self._repo.update_user(user)

    async def get_user_by_email(self, email: str) -> User | None:
        """按邮箱获取用户。

        Args:
            email: 用户邮箱地址。

        Returns:
            User 对象，或 None（未找到）。
        """
        return await self._repo.get_user_by_email(email)
