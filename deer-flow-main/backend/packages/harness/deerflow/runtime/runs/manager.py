"""内存运行注册表。

管理 Agent 运行记录的创建、状态转换和取消操作，所有变更受 asyncio 锁保护。
支持多任务策略：reject（拒绝并发）、interrupt（中断已有）、rollback（回滚）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .schemas import DisconnectMode, RunStatus

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunRecord:
    """单次运行的可变记录。"""

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


class RunManager:
    """内存运行注册表，所有变更受 asyncio 锁保护。"""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

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
        """创建新的待处理运行并注册。"""
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
        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        """根据 ID 返回运行记录。"""
        return self._runs.get(run_id)

    async def list_by_thread(self, thread_id: str) -> list[RunRecord]:
        """返回指定线程的所有运行记录（最新优先）。"""
        async with self._lock:
            return sorted(
                (r for r in self._runs.values() if r.thread_id == thread_id),
                key=lambda r: r.created_at,
                reverse=True,
            )

    async def set_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        """转换运行状态。"""
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                logger.warning("set_status called for unknown run %s", run_id)
                return
            record.status = status
            record.updated_at = _now_iso()
            if error is not None:
                record.error = error
        logger.info("Run %s -> %s", run_id, status.value)

    async def cancel(self, run_id: str, *, action: str = "interrupt") -> bool:
        """请求取消运行。

        Args:
            action: "interrupt" 保留检查点，"rollback" 回滚到运行前状态。
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
    ) -> RunRecord:
        """原子性检查并创建运行（消除 TOCTOU 竞态）。

        - reject 策略：线程已有运行时抛出 ConflictError
        - interrupt/rollback 策略：先取消已有运行再创建
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
            )
            self._runs[run_id] = record

        logger.info("Run created: run_id=%s thread_id=%s", run_id, thread_id)
        return record

    async def has_inflight(self, thread_id: str) -> bool:
        """检查线程是否有进行中的运行。"""
        async with self._lock:
            return any(r.thread_id == thread_id and r.status in (RunStatus.pending, RunStatus.running) for r in self._runs.values())

    async def cleanup(self, run_id: str, *, delay: float = 300) -> None:
        """延迟移除运行记录。"""
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            self._runs.pop(run_id, None)
        logger.debug("Run record %s cleaned up", run_id)


class ConflictError(Exception):
    """reject 策略下线程已有进行中运行时抛出。"""


class UnsupportedStrategyError(Exception):
    """多任务策略值未实现时抛出。"""
