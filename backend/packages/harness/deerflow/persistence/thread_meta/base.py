"""线程元数据存储的抽象接口。

定义了 ThreadMetaStore 抽象基类，所有线程元数据操作都需要通过此接口进行。
提供两种实现:
  - ThreadMetaRepository: 基于 SQL（sqlite / postgres 通过 SQLAlchemy）
  - MemoryThreadMetaStore: 包装 LangGraph BaseStore（内存模式）

所有修改和查询方法都接受 user_id 参数，具有三态语义
（参见 :mod:`deerflow.runtime.user_context`）:
  - AUTO（默认）: 从请求作用域的 contextvar 自动解析用户 ID
  - 显式 str:    使用提供的值
  - 显式 None:   绕过所有者过滤（仅用于迁移/CLI 场景）

这种三态设计允许在认证环境和非认证环境中复用同一套代码。
"""

from __future__ import annotations

import abc
from typing import Any

from deerflow.runtime.user_context import AUTO, _AutoSentinel


class InvalidMetadataFilterError(ValueError):
    """当客户端提供的所有元数据过滤键都被拒绝时抛出。

    作用：当所有过滤键都不符合安全要求时，明确告知调用方
    过滤条件无效，而不是静默忽略（静默忽略可能返回意外的大量数据）。
    """


class ThreadMetaStore(abc.ABC):
    """线程元数据存储的抽象基类。

    定义了所有线程元数据操作的标准接口，包括:
      - create:          创建线程元数据
      - get:             获取单个线程元数据
      - search:          搜索线程（支持元数据和状态过滤）
      - update_display_name: 更新线程标题
      - update_status:      更新线程状态
      - update_metadata:    合并更新线程自定义元数据
      - check_access:       检查用户访问权限
      - delete:             删除线程元数据

    所有方法都是异步的，具体实现由子类提供。
    """

    @abc.abstractmethod
    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """创建线程元数据记录。"""
        pass

    @abc.abstractmethod
    async def get(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
        """获取线程元数据。包含所有者过滤。"""
        pass

    @abc.abstractmethod
    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict[str, Any]]:
        """搜索线程，支持元数据和状态过滤。"""
        pass

    @abc.abstractmethod
    async def update_display_name(self, thread_id: str, display_name: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """更新线程的显示名称（标题）。"""
        pass

    @abc.abstractmethod
    async def update_status(self, thread_id: str, status: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """更新线程状态。"""
        pass

    @abc.abstractmethod
    async def update_metadata(self, thread_id: str, metadata: dict, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """合并 metadata 到线程的元数据字段。

        已有的键会被新值覆盖；metadata 中不存在的键保持不变。
        如果线程不存在或所有者检查失败，此操作为空操作。
        """
        pass

    @abc.abstractmethod
    async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False) -> bool:
        """检查 user_id 是否有权访问 thread_id。

        Args:
            require_existing: 为 True 时，只有当行存在且匹配时才返回 True。
                              用于删除等破坏性操作，防止删除已不存在的线程。
        """
        pass

    @abc.abstractmethod
    async def delete(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """删除线程元数据。"""
        pass
