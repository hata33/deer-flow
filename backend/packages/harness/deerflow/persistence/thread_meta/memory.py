"""基于 LangGraph BaseStore 的内存线程元数据存储。

在 database.backend=memory 时使用。
委托给 LangGraph Store 的 ``("threads",)`` 命名空间 ——
与 Gateway 路由器使用的线程记录命名空间相同。

为什么使用 LangGraph BaseStore:
  在内存模式下没有 SQLAlchemy 引擎，无法使用 SQL 仓库。
  LangGraph 的 BaseStore 提供了基本的键值存储和搜索功能，
  可以作为内存模式下的替代方案。
"""

from __future__ import annotations

from typing import Any

from langgraph.store.base import BaseStore

from deerflow.persistence.thread_meta.base import ThreadMetaStore
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso, now_iso

# 线程元数据在 LangGraph Store 中的命名空间
# 使用元组形式 ("threads",) 作为命名空间路径
THREADS_NS: tuple[str, ...] = ("threads",)


class MemoryThreadMetaStore(ThreadMetaStore):
    """内存模式的线程元数据存储。

    使用 LangGraph BaseStore 作为底层存储引擎，
    将线程元数据序列化为字典存入 BaseStore。
    """

    def __init__(self, store: BaseStore) -> None:
        self._store = store

    async def _get_owned_record(
        self,
        thread_id: str,
        user_id: str | None | _AutoSentinel,
        method_name: str,
    ) -> dict | None:
        """获取记录并验证所有权。返回可变副本，或 None。

        作用：将"获取+验证"的通用逻辑抽取为内部方法，
        避免在每个公开方法中重复相同的所有权检查代码。
        """
        resolved = resolve_user_id(user_id, method_name=method_name)
        item = await self._store.aget(THREADS_NS, thread_id)
        if item is None:
            return None
        # 创建可变副本，避免直接修改 Store 中的数据
        record = dict(item.value)
        # 所有者检查：非 None 时只返回匹配用户的记录
        if resolved is not None and record.get("user_id") != resolved:
            return None
        return record

    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """创建线程元数据记录。

        构建包含所有字段的字典并存入 LangGraph Store。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="MemoryThreadMetaStore.create")
        now = now_iso()
        record: dict[str, Any] = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "user_id": resolved_user_id,
            "display_name": display_name,
            "status": "idle",           # 新建线程默认状态为 idle
            "metadata": metadata or {},
            "values": {},                # 预留给 LangGraph 状态快照
            "created_at": now,
            "updated_at": now,
        }
        await self._store.aput(THREADS_NS, thread_id, record)
        return record

    async def get(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
        """获取线程元数据。包含所有权验证。"""
        return await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.get")

    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> list[dict[str, Any]]:
        """搜索线程，支持元数据和状态过滤。

        将所有过滤条件合并为一个 filter 字典传给 BaseStore 的 asearch。
        BaseStore 内部会按所有键值对进行匹配。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="MemoryThreadMetaStore.search")
        filter_dict: dict[str, Any] = {}
        if metadata:
            filter_dict.update(metadata)
        if status:
            filter_dict["status"] = status
        if resolved_user_id is not None:
            filter_dict["user_id"] = resolved_user_id

        items = await self._store.asearch(
            THREADS_NS,
            filter=filter_dict or None,
            limit=limit,
            offset=offset,
        )
        return [self._item_to_dict(item) for item in items]

    async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False) -> bool:
        """检查用户是否有权访问线程。

        两种模式:
          - require_existing=False（宽松）：记录不存在也返回 True（兼容未追踪的遗留线程）
          - require_existing=True（严格）：记录必须存在且所有者匹配

        宽松模式用于读取操作（如查看线程），
        严格模式用于破坏性操作（如删除线程）。
        """
        item = await self._store.aget(THREADS_NS, thread_id)
        if item is None:
            return not require_existing  # 不存在时：宽松返回 True，严格返回 False
        record_user_id = item.value.get("user_id")
        if record_user_id is None:
            return True  # 无所有者的记录（共享/认证前数据）允许任何用户访问
        return record_user_id == user_id

    async def update_display_name(self, thread_id: str, display_name: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """更新线程标题。先验证所有权，再更新后写回 Store。"""
        record = await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.update_display_name")
        if record is None:
            return
        record["display_name"] = display_name
        record["updated_at"] = now_iso()
        await self._store.aput(THREADS_NS, thread_id, record)

    async def update_status(self, thread_id: str, status: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """更新线程状态。"""
        record = await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.update_status")
        if record is None:
            return
        record["status"] = status
        record["updated_at"] = now_iso()
        await self._store.aput(THREADS_NS, thread_id, record)

    async def update_metadata(self, thread_id: str, metadata: dict, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """合并更新线程的自定义元数据。

        将新 metadata 合并到已有 metadata 中（浅合并），
        已有的键被新值覆盖，不存在的键保持不变。
        """
        record = await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.update_metadata")
        if record is None:
            return
        # 浅合并：新值覆盖旧值，旧值中不在新值中的键保留
        merged = dict(record.get("metadata") or {})
        merged.update(metadata)
        record["metadata"] = merged
        record["updated_at"] = now_iso()
        await self._store.aput(THREADS_NS, thread_id, record)

    async def update_owner(self, thread_id: str, owner_user_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        record = await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.update_owner")
        if record is None:
            return
        record["user_id"] = owner_user_id
        record["updated_at"] = now_iso()
        await self._store.aput(THREADS_NS, thread_id, record)

    async def delete(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> None:
        """删除线程元数据。先验证所有权再删除。"""
        record = await self._get_owned_record(thread_id, user_id, "MemoryThreadMetaStore.delete")
        if record is None:
            return
        await self._store.adelete(THREADS_NS, thread_id)

    @staticmethod
    def _item_to_dict(item) -> dict[str, Any]:
        """将 Store 的 SearchItem 转换为调用方期望的字典格式。

        作用：统一内存和 SQL 实现的输出格式，
        使上层代码无需关心底层存储实现。
        """
        val = item.value
        return {
            "thread_id": item.key,
            "assistant_id": val.get("assistant_id"),
            "user_id": val.get("user_id"),
            "display_name": val.get("display_name"),
            "status": val.get("status", "idle"),
            "metadata": val.get("metadata", {}),
            # coerce_iso 修复早期 Gateway 版本写入的 unix 时间戳格式
            # （早期版本使用 str(time.time()) 而非 ISO 格式）
            "created_at": coerce_iso(val.get("created_at", "")),
            "updated_at": coerce_iso(val.get("updated_at", "")),
        }
