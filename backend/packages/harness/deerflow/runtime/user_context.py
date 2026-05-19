"""
基于用户授权的请求范围用户上下文模块。

该模块保存一个 :class:`~contextvars.ContextVar`，网关的认证中间件
在成功认证后设置它。Repository 方法通过哨兵默认参数读取 contextvar，
让路由器免于 ``user_id`` 样板代码。

Repository ``user_id`` 参数的三态语义（此模块的使用者端位于
``deerflow.persistence.*``）：

- ``_AUTO``（模块私有哨兵，默认）：从 contextvar 读取；
  如果未设置则引发 :class:`RuntimeError`。
- 显式 ``str``：使用提供的值，覆盖 contextvar。
- 显式 ``None``：无 WHERE 子句 —— 仅由有意绕过隔离的
  迁移脚本和管理 CLI 使用。

依赖方向
--------
``persistence``（下层）从此模块读取；``gateway.auth``（上层）
写入它。``CurrentUser`` 在这里定义为 :class:`typing.Protocol`，
因此 ``persistence`` 永远不需要从 ``gateway.auth.models`` 导入
具体的 ``User`` 类。任何具有 ``.id: str`` 属性的对象在结构上
都满足该协议。

Asyncio 语义
-------------
``ContextVar`` 在 asyncio 下是任务本地的，而非线程本地的。
每个 FastAPI 请求在自己的任务中运行，因此上下文自然是隔离的。
``asyncio.create_task`` 和 ``asyncio.to_thread`` 继承父任务的上下文，
这通常是预期的行为；如果后台任务必须*不*看到前台用户，
请使用 ``contextvars.copy_context()`` 包装它以获得干净的副本。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Final, Protocol, runtime_checkable


@runtime_checkable
class CurrentUser(Protocol):
    """当前认证用户的结构类型。

    任何具有 ``.id: str`` 属性的对象都满足此协议。
    具体实现位于 ``app.gateway.auth.models.User``。
    """

    id: str


# 当前用户的上下文变量
_current_user: Final[ContextVar[CurrentUser | None]] = ContextVar("deerflow_current_user", default=None)


def set_current_user(user: CurrentUser) -> Token[CurrentUser | None]:
    """为此异步任务设置当前用户。

    Args:
        user: 要设置的用户对象

    Returns:
        应该在 ``finally`` 块中传递给 :func:`reset_current_user` 的
        重置令牌，以恢复先前的上下文。
    """
    return _current_user.set(user)


def reset_current_user(token: Token[CurrentUser | None]) -> None:
    """将上下文恢复到 ``token`` 捕获的状态。

    Args:
        token: 由 set_current_user 返回的令牌
    """
    _current_user.reset(token)


def get_current_user() -> CurrentUser | None:
    """返回当前用户，如果未设置则返回 ``None``。

    Returns:
        当前用户对象或 None

    Note:
        在任何上下文中调用都是安全的。由可以在没有用户的情况下
        继续的代码路径使用（如迁移脚本、公共端点）。
    """
    return _current_user.get()


def require_current_user() -> CurrentUser:
    """返回当前用户，或引发 :class:`RuntimeError`。

    Returns:
        当前用户对象

    Raises:
        RuntimeError: 如果没有用户上下文

    Note:
        由绝不能在请求认证上下文之外调用的 repository 代码使用。
        错误消息的措辞使调用者调试堆栈跟踪时可以定位有问题的代码路径。
    """
    user = _current_user.get()
    if user is None:
        raise RuntimeError("repository accessed without user context")
    return user


# ---------------------------------------------------------------------------
# 有效 user_id 助手（文件系统隔离）
# ---------------------------------------------------------------------------

DEFAULT_USER_ID: Final[str] = "default"


def get_effective_user_id() -> str:
    """返回当前用户的 id 作为字符串，如果未设置则返回 DEFAULT_USER_ID。

    Returns:
        用户 ID 字符串或默认用户 ID

    Note:
        与 :func:`require_current_user` 不同，这永远不会引发异常 ——
        它专为总是需要有效用户桶的文件系统路径解析而设计。
    """
    user = _current_user.get()
    if user is None:
        return DEFAULT_USER_ID
    return str(user.id)


# ---------------------------------------------------------------------------
# 基于哨兵的 user_id 解析
# ---------------------------------------------------------------------------
#
# Repository 方法接受一个默认为 ``AUTO`` 的仅限关键字的 ``user_id`` 参数。
# 三个可能的值驱动不同的行为；参见 :func:`resolve_user_id` 上的文档字符串。


class _AutoSentinel:
    """单例标记，表示"从 contextvar 解析 user_id"。"""

    _instance: _AutoSentinel | None = None

    def __new__(cls) -> _AutoSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<AUTO>"


# 自动解析 user_id 的哨兵值
AUTO: Final[_AutoSentinel] = _AutoSentinel()


def resolve_user_id(
    value: str | None | _AutoSentinel,
    *,
    method_name: str = "repository method",
) -> str | None:
    """解析传递给 repository 方法的 user_id 参数。

    三态语义：

    - :data:`AUTO`（默认）：从 contextvar 读取；如果上下文中没有用户，
      则引发 :class:`RuntimeError`。这是请求范围调用的常见情况。
    - 显式 ``str``：逐字使用提供的 id，覆盖任何 contextvar 值。
      对测试和管理覆盖流很有用。
    - 显式 ``None``：无过滤器 —— repository 应该完全跳过
      user_id WHERE 子句。保留给有意绕过隔离的迁移脚本和 CLI 工具。

    Args:
        value: user_id 参数值
        method_name: 用于错误消息的方法名称

    Returns:
        解析后的 user_id 字符串或 None

    Raises:
        RuntimeError: 如果 value 是 AUTO 但没有设置用户上下文
    """
    if isinstance(value, _AutoSentinel):
        user = _current_user.get()
        if user is None:
            raise RuntimeError(f"{method_name} called with user_id=AUTO but no user context is set; pass an explicit user_id, set the contextvar via auth middleware, or opt out with user_id=None for migration/CLI paths.")
        # 在边界强制转换为 ``str``：``User.id`` 在 API 表面类型为 ``UUID``，
        # 但持久层将 ``user_id`` 存储为 ``String(64)``，aiosqlite 不能将原始
        # UUID 对象绑定到 VARCHAR 列（"type 'UUID' is not supported"）。
        # 在这里遵守文档化的返回类型，而不是通过每个调用者传播类型更改。
        return str(user.id)
    return value
