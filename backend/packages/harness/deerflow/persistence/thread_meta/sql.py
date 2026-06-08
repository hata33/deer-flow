"""基于 SQLAlchemy 的线程元数据仓库。

实现 ThreadMetaStore 抽象接口，提供线程元数据的完整 CRUD 操作。
每个方法获取独立短生命周期的会话，操作完成后立即释放。

本仓库的核心功能:
  - 线程的创建、查询、搜索、更新和删除
  - 基于 JSON 元数据的过滤搜索（使用 json_compat 模块）
  - 用户所有权验证（所有修改和查询操作都包含所有者过滤）
  - 元数据的合并更新（read-modify-write 模式保证一致性）
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.json_compat import json_match
from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)


class ThreadMetaRepository(ThreadMetaStore):
    """SQL 实现的线程元数据仓库。

    通过 async_sessionmaker 创建短生命周期会话，
    确保每个操作在独立事务中执行。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _row_to_dict(row: ThreadMetaRow) -> dict[str, Any]:
        """将 ORM 行转换为字典，重映射 JSON 列名并处理时间格式。

        重映射:
          - metadata_json → metadata（与 ThreadMetaStore 接口保持一致）
          - datetime → ISO 字符串（与 MemoryThreadMetaStore 格式一致）
        """
        d = row.to_dict()
        d["metadata"] = d.pop("metadata_json", None) or {}
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                # SQLite drops tzinfo despite ``DateTime(timezone=True)``;
                # ``coerce_iso`` normalizes naive values as UTC so the wire format always carries tz.
                d[key] = coerce_iso(val)
        return d

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

        AUTO 模式自动从 contextvar 解析用户 ID；
        显式 None 创建无主记录（用于迁移脚本）。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.create")
        now = datetime.now(UTC)
        row = ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=assistant_id,
            user_id=resolved_user_id,
            display_name=display_name,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)  # 刷新以获取数据库默认值
            return self._row_to_dict(row)

    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> dict | None:
        """获取线程元数据。包含所有者过滤。"""
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.get")
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return None
            # 强制所有者过滤：非 None 时只返回匹配用户的记录
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return None
            return self._row_to_dict(row)

    async def check_access(self, thread_id: str, user_id: str, *, require_existing: bool = False) -> bool:
        """检查用户是否有权访问线程。

        两种模式对应不同的安全策略:

        - require_existing=False（宽松，默认）:
          用于读取操作。以下情况返回 True:
            * 记录不存在（兼容未追踪的遗留线程）
            * user_id 为 None（共享/认证前数据）
            * user_id 匹配
          宽松模式保持向后兼容。

        - require_existing=True（严格）:
          用于破坏性操作（DELETE、PATCH）。
          记录必须存在且所有者匹配才返回 True。
          防止已删除的线程被其他用户重新操作。
        """
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return not require_existing  # 不存在：宽松=True，严格=False
            if row.user_id is None:
                return True  # 无主记录允许任何用户访问
            return row.user_id == user_id

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

        过滤逻辑:
          1. 用户所有权过滤（默认启用，显式 user_id=None 绕过）
          2. 状态过滤（可选）
          3. 元数据过滤（使用 json_match 实现跨数据库兼容）

        元数据过滤的安全性:
          每个键值对都通过 validate_metadata_filter_key/value 验证。
          不安全的键会被跳过并记录警告。
          如果所有键都不安全，抛出 InvalidMetadataFilterError。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.search")
        # 按更新时间降序排列（最近更新的在前），相同时间按 thread_id 降序
        stmt = select(ThreadMetaRow).order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
        if resolved_user_id is not None:
            stmt = stmt.where(ThreadMetaRow.user_id == resolved_user_id)
        if status:
            stmt = stmt.where(ThreadMetaRow.status == status)

        if metadata:
            applied = 0  # 记录成功应用的过滤器数量
            for key, value in metadata.items():
                try:
                    # 使用 json_match 构建跨数据库兼容的 JSON 过滤谓词
                    stmt = stmt.where(json_match(ThreadMetaRow.metadata_json, key, value))
                    applied += 1
                except (ValueError, TypeError) as exc:
                    # 不安全的键被跳过，记录警告
                    logger.warning("Skipping metadata filter key %s: %s", ascii(key), exc)
            if applied == 0:
                # 所有键都不安全 → 抛出异常，避免返回意外的大量数据
                rejected_keys = ", ".join(sorted(str(k) for k in metadata))
                raise InvalidMetadataFilterError(f"All metadata filter keys were rejected as unsafe: {rejected_keys}")

        stmt = stmt.limit(limit).offset(offset)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def _check_ownership(self, session: AsyncSession, thread_id: str, resolved_user_id: str | None) -> bool:
        """检查记录是否存在且属于指定用户。

        内部辅助方法，用于更新和删除操作前的所有权验证。
        resolved_user_id 为 None 时绕过检查（迁移/CLI 场景）。
        """
        if resolved_user_id is None:
            return True  # 显式绕过
        row = await session.get(ThreadMetaRow, thread_id)
        return row is not None and row.user_id == resolved_user_id

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """更新线程的显示名称（标题）。

        先验证所有权，再执行 UPDATE。如果验证失败则静默跳过。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_display_name")
        async with self._sf() as session:
            if not await self._check_ownership(session, thread_id, resolved_user_id):
                return
            await session.execute(update(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id).values(display_name=display_name, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """更新线程状态。先验证所有权再执行 UPDATE。"""
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_status")
        async with self._sf() as session:
            if not await self._check_ownership(session, thread_id, resolved_user_id):
                return
            await session.execute(update(ThreadMetaRow).where(ThreadMetaRow.thread_id == thread_id).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """合并 metadata 到 metadata_json。

        使用 read-modify-write 模式:
          1. 读取当前 metadata_json
          2. 在 Python 中合并新值
          3. 写回数据库

        整个操作在单个会话/事务中完成，保证并发调用者看到一致的状态。
        如果记录不存在或所有者检查失败，此操作为空操作。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_metadata")
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return
            # 浅合并：新值覆盖旧值，旧值中不在新值中的键保留
            merged = dict(row.metadata_json or {})
            merged.update(metadata)
            row.metadata_json = merged
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> None:
        """删除线程元数据。先验证所有权再删除。"""
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.delete")
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None:
                return
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return
            await session.delete(row)
            await session.commit()
