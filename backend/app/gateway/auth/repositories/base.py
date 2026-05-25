"""用户仓库抽象接口 — 定义用户数据存储的标准操作。

本模块定义了 UserRepository 抽象基类和 UserNotFoundError 异常，
是认证子系统的数据访问层接口。

核心设计：
  - 所有方法都是异步的，与 FastAPI 的异步架构一致
  - UserNotFoundError 继承自 LookupError，兼容已有的异常处理逻辑
  - 接口设计遵循最小化原则，只包含认证所需的核心操作

接口方法一览：
  - create_user       — 创建新用户（邮箱重复抛 ValueError）
  - get_user_by_id    — 按 ID 查找用户
  - get_user_by_email — 按邮箱查找用户
  - update_user       — 更新用户（行不存在抛 UserNotFoundError）
  - count_users       — 统计用户总数
  - count_admin_users — 统计管理员数量
  - get_user_by_oauth — 按 OAuth 提供者和 ID 查找用户

实现类：
  - SQLiteUserRepository（sqlite.py）— 唯一的当前实现

错误处理策略：
  - create_user 在邮箱重复时抛 ValueError（业务约束违反）
  - update_user 在行不存在时抛 UserNotFoundError（并发删除检测）
  - 查询方法返回 None 表示未找到（而非抛异常）
"""

from abc import ABC, abstractmethod

from app.gateway.auth.models import User


class UserNotFoundError(LookupError):
    """用户仓库操作目标行不存在时抛出。

    继承 LookupError，使已经捕获 LookupError 处理"实体不存在"的
    调用者无需修改。特定调用点可以绑定此类来区分"更新期间并发删除"
    和其他查找操作。
    """


class UserRepository(ABC):
    """用户数据存储抽象接口。

    实现此接口以支持不同的存储后端（SQLite 等）。
    所有方法都是异步的。
    """

    @abstractmethod
    async def create_user(self, user: User) -> User:
        """创建新用户。

        Args:
            user: 要创建的 User 对象。

        Returns:
            创建的 User（已分配 ID）。

        Raises:
            ValueError: 邮箱已存在。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_user_by_id(self, user_id: str) -> User | None:
        """按 ID 获取用户。

        Args:
            user_id: 用户 UUID 字符串。

        Returns:
            找到时返回 User，否则返回 None。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_user_by_email(self, email: str) -> User | None:
        """按邮箱获取用户。

        Args:
            email: 用户邮箱地址。

        Returns:
            找到时返回 User，否则返回 None。
        """
        raise NotImplementedError

    @abstractmethod
    async def update_user(self, user: User) -> User:
        """更新已有用户。

        Args:
            user: 包含更新字段的 User 对象。

        Returns:
            更新后的 User。

        Raises:
            UserNotFoundError: user.id 对应的行不存在。
                这是硬失败（非空操作），确保调用者不会将并发删除误认为更新成功。
        """
        raise NotImplementedError

    @abstractmethod
    async def count_users(self) -> int:
        """返回注册用户总数。"""
        raise NotImplementedError

    @abstractmethod
    async def count_admin_users(self) -> int:
        """返回 system_role == 'admin' 的用户数量。"""
        raise NotImplementedError

    @abstractmethod
    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> User | None:
        """按 OAuth 提供者和 ID 获取用户。

        Args:
            provider: OAuth 提供者名称（如 'github'、'google'）。
            oauth_id: OAuth 提供者中的用户 ID。

        Returns:
            找到时返回 User，否则返回 None。
        """
        raise NotImplementedError
