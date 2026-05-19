"""
JSONL 文件支持的 RunEventStore 实现。

每个运行的事件存储在单个文件中：
``.deer-flow/threads/{thread_id}/runs/{run_id}.jsonl``

所有类别（message、trace、lifecycle）都在同一文件中。
此后端适用于轻量级单节点部署。

已知权衡：``list_messages()`` 必须扫描线程的所有运行文件，
因为来自多个运行的消息需要统一的 seq 排序。
``list_events()`` 仅读取一个文件 —— 快速路径。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from deerflow.runtime.events.store.base import RunEventStore

logger = logging.getLogger(__name__)

# 安全 ID 模式：仅允许字母数字、连字符和下划线
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


class JsonlRunEventStore(RunEventStore):
    """JSONL 文件支持的运行事件存储实现。

    将每个运行的事件存储在单独的 JSONL 文件中，适用于轻量级部署。
    """

    def __init__(self, base_dir: str | Path | None = None):
        """初始化 JSONL 运行事件存储。

        Args:
            base_dir: 基础目录路径，默认为 ".deer-flow"
        """
        self._base_dir = Path(base_dir) if base_dir else Path(".deer-flow")
        self._seq_counters: dict[str, int] = {}  # thread_id -> 当前最大 seq

    @staticmethod
    def _validate_id(value: str, label: str) -> str:
        """验证 ID 对于文件系统路径使用是否安全。

        Args:
            value: 要验证的 ID 值
            label: 标签名称（用于错误消息）

        Returns:
            验证后的 ID

        Raises:
            ValueError: 如果 ID 包含不安全字符
        """
        if not value or not _SAFE_ID_PATTERN.match(value):
            raise ValueError(f"Invalid {label}: must be alphanumeric/dash/underscore, got {value!r}")
        return value

    def _thread_dir(self, thread_id: str) -> Path:
        """获取线程目录路径。

        Args:
            thread_id: 线程 ID

        Returns:
            线程目录路径
        """
        self._validate_id(thread_id, "thread_id")
        return self._base_dir / "threads" / thread_id / "runs"

    def _run_file(self, thread_id: str, run_id: str) -> Path:
        """获取运行文件路径。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID

        Returns:
            运行文件路径
        """
        self._validate_id(run_id, "run_id")
        return self._thread_dir(thread_id) / f"{run_id}.jsonl"

    def _next_seq(self, thread_id: str) -> int:
        """获取线程的下一个序列号。

        Args:
            thread_id: 线程 ID

        Returns:
            下一个序列号
        """
        self._seq_counters[thread_id] = self._seq_counters.get(thread_id, 0) + 1
        return self._seq_counters[thread_id]

    def _ensure_seq_loaded(self, thread_id: str) -> None:
        """如果尚未缓存，则从现有文件加载最大 seq。

        Args:
            thread_id: 线程 ID
        """
        if thread_id in self._seq_counters:
            return
        max_seq = 0
        thread_dir = self._thread_dir(thread_id)
        if thread_dir.exists():
            for f in thread_dir.glob("*.jsonl"):
                for line in f.read_text(encoding="utf-8").strip().splitlines():
                    try:
                        record = json.loads(line)
                        max_seq = max(max_seq, record.get("seq", 0))
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed JSONL line in %s", f)
                        continue
        self._seq_counters[thread_id] = max_seq

    def _write_record(self, record: dict) -> None:
        """将记录写入 JSONL 文件。

        Args:
            record: 要写入的事件记录
        """
        path = self._run_file(record["thread_id"], record["run_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")

    def _read_thread_events(self, thread_id: str) -> list[dict]:
        """读取线程的所有事件，按 seq 排序。

        Args:
            thread_id: 线程 ID

        Returns:
            排序后的事件列表
        """
        events = []
        thread_dir = self._thread_dir(thread_id)
        if not thread_dir.exists():
            return events
        for f in sorted(thread_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").strip().splitlines():
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed JSONL line in %s", f)
                    continue
        events.sort(key=lambda e: e.get("seq", 0))
        return events

    def _read_run_events(self, thread_id: str, run_id: str) -> list[dict]:
        """读取特定运行文件的事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID

        Returns:
            排序后的事件列表
        """
        path = self._run_file(thread_id, run_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("Skipping malformed JSONL line in %s", path)
                continue
        events.sort(key=lambda e: e.get("seq", 0))
        return events

    async def put(self, *, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None):
        """写入单个事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_type: 事件类型
            category: 事件类别
            content: 事件内容
            metadata: 元数据
            created_at: 创建时间

        Returns:
            完整的事件记录
        """
        self._ensure_seq_loaded(thread_id)
        seq = self._next_seq(thread_id)
        record = {
            "thread_id": thread_id,
            "run_id": run_id,
            "event_type": event_type,
            "category": category,
            "content": content,
            "metadata": metadata or {},
            "seq": seq,
            "created_at": created_at or datetime.now(UTC).isoformat(),
        }
        self._write_record(record)
        return record

    async def put_batch(self, events):
        """批量写入事件。

        Args:
            events: 事件字典列表

        Returns:
            完整的事件记录列表
        """
        if not events:
            return []
        results = []
        for ev in events:
            record = await self.put(**ev)
            results.append(record)
        return results

    async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None):
        """返回线程的可显示消息。

        Args:
            thread_id: 线程 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的记录
            after_seq: 返回 seq > after_seq 的记录

        Returns:
            消息字典列表
        """
        all_events = self._read_thread_events(thread_id)
        messages = [e for e in all_events if e.get("category") == "message"]

        if before_seq is not None:
            messages = [e for e in messages if e["seq"] < before_seq]
            return messages[-limit:]
        elif after_seq is not None:
            messages = [e for e in messages if e["seq"] > after_seq]
            return messages[:limit]
        else:
            return messages[-limit:]

    async def list_events(self, thread_id, run_id, *, event_types=None, limit=500):
        """返回运行的完整事件流。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            event_types: 可选的事件类型过滤器
            limit: 返回记录数量限制

        Returns:
            事件字典列表
        """
        events = self._read_run_events(thread_id, run_id)
        if event_types is not None:
            events = [e for e in events if e.get("event_type") in event_types]
        return events[:limit]

    async def list_messages_by_run(self, thread_id, run_id, *, limit=50, before_seq=None, after_seq=None):
        """返回特定运行的可显示消息。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID
            limit: 返回记录数量限制
            before_seq: 返回 seq < before_seq 的记录
            after_seq: 返回 seq > after_seq 的记录

        Returns:
            消息字典列表
        """
        events = self._read_run_events(thread_id, run_id)
        filtered = [e for e in events if e.get("category") == "message"]
        if before_seq is not None:
            filtered = [e for e in filtered if e.get("seq", 0) < before_seq]
        if after_seq is not None:
            filtered = [e for e in filtered if e.get("seq", 0) > after_seq]
        if after_seq is not None:
            return filtered[:limit]
        else:
            return filtered[-limit:] if len(filtered) > limit else filtered

    async def count_messages(self, thread_id):
        """计算线程中的可显示消息数量。

        Args:
            thread_id: 线程 ID

        Returns:
            消息数量
        """
        all_events = self._read_thread_events(thread_id)
        return sum(1 for e in all_events if e.get("category") == "message")

    async def delete_by_thread(self, thread_id):
        """删除线程的所有事件。

        Args:
            thread_id: 线程 ID

        Returns:
            删除的事件数量
        """
        all_events = self._read_thread_events(thread_id)
        count = len(all_events)
        thread_dir = self._thread_dir(thread_id)
        if thread_dir.exists():
            for f in thread_dir.glob("*.jsonl"):
                f.unlink()
        self._seq_counters.pop(thread_id, None)
        return count

    async def delete_by_run(self, thread_id, run_id):
        """删除特定运行的所有事件。

        Args:
            thread_id: 线程 ID
            run_id: 运行 ID

        Returns:
            删除的事件数量
        """
        events = self._read_run_events(thread_id, run_id)
        count = len(events)
        path = self._run_file(thread_id, run_id)
        if path.exists():
            path.unlink()
        return count
