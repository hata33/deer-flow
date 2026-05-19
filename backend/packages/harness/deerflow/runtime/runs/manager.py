"""
带有可选持久化 RunStore 支持的内存运行注册表。

提供运行记录的内存管理，可选择将运行元数据持久化到存储后端。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from deerflow.utils.time import now_iso as _now_iso

from .schemas import DisconnectMode, RunStatus

if TYPE_CHECKING:
    from deerflow.runtime.runs.store.base import RunStore

logger = logging.getLogger(__name__)


@dataclass
class RunRecord:
    """单个运行的可变记录。

    Attributes:
        run_id: 运行唯一标识符
        thread_id: 线程 ID
        assistant_id: 助手 ID
        status: 运行状态
        on_disconnect: 断开连接模式
        multitask_strategy: 多任务策略
        metadata: 元数据字典
        kwargs: 关键字参数字典
        created_at: 创建时间（ISO 格式）
        updated_at: 更新时间（ISO 格式）
        task: 异步任务对象
        abort_event: 中止事件
        abort_action: 中止操作类型
        error: 错误信息
        model_name: 模型名称
    """

    run_id: str
    thread_id: str
    assistant_id: str | None
    status: RunStatus
    on_disconnect: DisconnectMode
    multitask_strategy: str = "reject"
    metadata: dict = field(default_factory=dict)
    kwargs: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    abort_action: str = "interrupt"
    error: str | None = None
    model_name: str | None = None


class RunManager:
    """带有可选持久化 RunStore 支持的内存运行注册表。

    所有突变都由 asyncio 锁保护。当提供 ``store`` 时，
    可序列化的元数据也会持久化到存储，以便运行历史在进程重启后幸存。

    Attributes:
        _runs: 运行记录字典
        _lock: 异步锁
        _store: 可选的运行存储后端
    """

    def __init__(self, store: RunStore | None = None) -> None:
        """初始化运行管理器。

        Args:
            store: 可选的运行存储后端
        """
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()
        self._store = store

    async def _persist_to_store(self, record: RunRecord) -> None:
        """尽最大努力将运行记录持久化到后备存储。

        Args:
            record: 要持久化的运行记录
        """
        if self._store is None:
            return
        try:
            await self._store.put(
                record.run_id,
                thread_id=record.thread_id,
                assistant_id=record.assistant_id,
                status=record.status.value,
                multitask_strategy=record.multitask_strategy,
                metadata=record.metadata or {},
                kwargs=record.kwargs or {},
                created_at=record.created_at,
                model_name=record.model_name,
            )
        except Exception:
            logger.warning("Failed to persist run %s to store", record.run_id, exc_info=True)

    async def update_run_completion(self, run_id: str, **kwargs) -> None:
        """将 token 使用量和完成数据持久化到后备存储。

        Args:
            run_id: 运行 ID
            **kwargs: 完成数据
        """
        if self._store is not None:
            try:
                await self._store.update_run_completion(run_id, **kwargs)
            except Exception:
                logger.warning("Failed to persist run completion for %s", run_id, exc_info=True)

    async def create(
        self,
        thread_id: str,
        assistant_id: str | None = None,
        *,
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        metadata: dict | None = None,
        kwargs: dict | None = None,
        multitask_strategy: str = "reject",
    ) -> RunRecord:
        """创建新的待处理运行并注册它。

        Args:
            thread_id: 线程 ID
            assistant_id: 助手 ID
            on_disconnect: 断开连接模式
            metadata: 元数据
            kwargs: 关键字参数
            multitask_strategy: 多任务策略

        Returns:
            创建的运行记录
        """
        run_id = str(uuid.uuid4())
        now = _now_iso()
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=assistant_id,
            status=RunStatus.pending,
            on_disconnect=on_disconnect,
            multitask_strategy=multitask_strategy,
            metadata=metadata or {},
            kwargs=kwargs or {},
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._runs[run_id] = record
        await self._persist_to_store(record)
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        """按 ID 返回运行记录，如果不存在则返回 ``None``。

        Args:
            run_id: 运行 ID

        Returns:
            运行记录或 None
        """
        return self._runs.get(run_id)

    async def list_by_thread(self, thread_id: str) -> list[RunRecord]:
        """返回给定线程的所有运行，最新的在前。

        Args:
            thread_id: 线程 ID

        Returns:
            运行记录列表

        Note:
            字典插入顺序与创建顺序匹配，因此反转它即使在时间戳平局时
            也能为我们提供确定性的最新优先结果。
        """
        async with self._lock:
            return [r for r in self._runs.values() if r.thread_id == thread_id]

    async def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        """将运行转换为新状态。

        Args:
            run_id: 运行 ID
            status: 新状态
            error: 可选的错误信息
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("set_status called for unknown run %s", run_id)
                return
            record.status = status
            record.updated_at = _now_iso()
            if error is not None:
                record.error = error
        if self._store is not None:
            try:
                await self._store.update_status(run_id, status.value, error=error)
            except Exception:
                logger.warning("Failed to persist status update for run %s", run_id, exc_info=True)
        logger.info("Run %s -> %s", run_id, status.value)

    async def update_model_name(self, run_id: str, model_name: str | None) -> None:
        """更新运行的模型名称。

        Args:
            run_id: 运行 ID
            model_name: 模型名称
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("update_model_name called for unknown run %s", run_id)
                return
            record.model_name = model_name
            record.updated_at = _now_iso()
        await self._persist_to_store(record)
        logger.info("Run %s model_name=%s", run_id, model_name)

    async def cancel(self, run_id: str, *, action: str = "interrupt") -> bool:
        """请求取消运行。

        Args:
            run_id: 要取消的运行 ID
            action: "interrupt" 保留检查点，"rollback" 恢复到运行前状态

        Returns:
            如果运行正在进行并已启动取消，则返回 ``True``

        Note:
            使用操作原因设置中止事件并取消 asyncio 任务。
        """
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return False
            if record.status not in (RunStatus.pending, RunStatus.running):
                return False
            record.abort_action = action
            record.abort_event.set()
            if record.task is not None and not record.task.done():
                record.task.cancel()
            record.status = RunStatus.interrupted
            record.updated_at = _now_iso()
        logger.info("Run %s cancelled (action=%s)", run_id, action)
        return True

    async def create_or_reject(
        self,
        thread_id: str,
        assistant_id: str | None = None,
        *,
        on_disconnect: DisconnectMode = DisconnectMode.cancel,
        metadata: dict | None = None,
        kwargs: dict | None = None,
        multitask_strategy: str = "reject",
        model_name: str | None = None,
    ) -> RunRecord:
        """原子地检查进行中的运行并创建新运行。

        Args:
            thread_id: 线程 ID
            assistant_id: 助手 ID
            on_disconnect: 断开连接模式
            metadata: 元数据
            kwargs: 关键字参数
            multitask_strategy: 多任务策略
            model_name: 模型名称

        Returns:
            创建的运行记录

        Raises:
            ConflictError: 如果策略为 "reject" 且线程已有活动运行
            UnsupportedStrategyError: 如果策略不受支持

        Note:
            对于 ``reject`` 策略，如果线程已有待处理/运行的运行，
            则引发 ``ConflictError``。对于 ``interrupt``/``rollback``，
            在创建前取消进行中的运行。

            此方法在检查和插入期间都持有锁，消除了单独的
            ``has_inflight`` + ``create`` 中的 TOCTOU 竞争。
        """
        run_id = str(uuid.uuid4())
        now = _now_iso()

        _supported_strategies = ("reject", "interrupt", "rollback")

        async with self._lock:
            if multitask_strategy not in _supported_strategies:
                raise UnsupportedStrategyError(f"Multitask strategy '{multitask_strategy}' is not yet supported. Supported strategies: {', '.join(_supported_strategies)}")

            inflight = [r for r in self._runs.values() if r.thread_id == thread_id and r.status in (RunStatus.pending, RunStatus.running)]

            if multitask_strategy == "reject" and inflight:
                raise ConflictError(f"Thread {thread_id} already has an active run")

            if multitask_strategy in ("interrupt", "rollback") and inflight:
                for r in inflight:
                    r.abort_action = multitask_strategy
                    r.abort_event.set()
                    if r.task is not None and not r.task.done():
                        r.task.cancel()
                    r.status = RunStatus.interrupted
                    r.updated_at = now
                logger.info(
                    "Cancelled %d inflight run(s) on thread %s (strategy=%s)",
                    len(inflight),
                    thread_id,
                    multitask_strategy,
                )

            record = RunRecord(
                run_id=run_id,
                thread_id=thread_id,
                assistant_id=assistant_id,
                status=RunStatus.pending,
                on_disconnect=on_disconnect,
                multitask_strategy=multitask_strategy,
                metadata=metadata or {},
                kwargs=kwargs or {},
                created_at=now,
                updated_at=now,
                model_name=model_name,
            )
            self._runs[run_id] = record

        await self._persist_to_store(record)
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    async def has_inflight(self, thread_id: str) -> bool:
        """如果 *thread_id* 有待处理或正在运行的运行，则返回 ``True``。

        Args:
            thread_id: 线程 ID

        Returns:
            是否有进行中的运行
        """
        async with self._lock:
            return any(r.thread_id == thread_id and r.status in (RunStatus.pending, RunStatus.running) for r in self._runs.values())

    async def cleanup(self, run_id: str, *, delay: float = 300) -> None:
        """在可选延迟后删除运行记录。

        Args:
            run_id: 运行 ID
            delay: 延迟秒数
        """
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            self._runs.pop(run_id, None)
        logger.debug("Run record %s cleaned up", run_id)


class ConflictError(Exception):
    """当 multitask_strategy=reject 且线程有进行中的运行时引发。"""


class UnsupportedStrategyError(Exception):
    """当 multitask_strategy 值尚未实现时引发。"""
