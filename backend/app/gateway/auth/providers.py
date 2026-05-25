"""认证提供者抽象基类 — 扩展认证方式的接口定义。

本模块定义了 AuthProvider 抽象基类，是 DeerFlow 认证子系统的
策略模式核心。所有认证实现（本地邮箱/密码、OAuth 等）都继承此类。

设计目标：
  - 统一认证接口：不同的认证方式实现相同的 authenticate 和 get_user 方法
  - 可扩展性：新增认证方式只需实现 AuthProvider 接口
  - 与存储解耦：Provider 只负责认证逻辑，用户数据通过 UserRepository 访问

当前实现：
  - LocalAuthProvider（local_provider.py）：邮箱/密码认证
  - 未来可扩展：OAuth（GitHub、Google）、LDAP、SAML 等

注意：
  - User 类型通过底部运行时导入避免循环导入
  - authenticate 返回 None 表示认证失败（而非抛异常）
"""

from abc import ABC, abstractmethod


class AuthProvider(ABC):
    """认证提供者抽象基类。

    所有认证实现必须继承此类并实现 authenticate 和 get_user 方法。
    """

    @abstractmethod
    async def authenticate(self, credentials: dict) -> "User | None":
        """使用给定的凭据认证用户。

        Args:
            credentials: 认证凭据字典，具体格式由子类定义。

        Returns:
            认证成功时返回 User，失败时返回 None。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_user(self, user_id: str) -> "User | None":
        """按 ID 获取用户。

        Args:
            user_id: 用户 UUID 字符串。

        Returns:
            User 对象，或 None（未找到）。
        """
        raise NotImplementedError


# 运行时导入 User 以避免循环导入
from app.gateway.auth.models import User  # noqa: E402
