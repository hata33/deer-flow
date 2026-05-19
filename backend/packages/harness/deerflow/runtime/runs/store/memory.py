"""
内存运行存储。当 database.backend=memory（默认）和测试中使用。

等同于原始的 RunManager._runs 字典行为。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from deerflow.runtime.runs.store.base import RunStore


class MemoryRunStore(RunStore):
    """内存运行存储实现。

    将运行记录存储在内存字典中，适用于开发和测试环境。
    """

    def __init__(self) -> None:
        """初始化内存运行存储。"""
        self._runs: dict[str, dict[str, Any]] = {}

    async def put(
        self,
        run_id,
        *,
        thread_id,
        assistant_id=None,
        user_id=None,
        model_name=None,
        status="pending",
        multitask_strategy="reject",
        metadata=None,
        kwargs=None,
        error=None,
        created_at=None,
    ):
        """存储运行记录。

        Args:
            run_id: 运行 ID
            thread_id: 线程 ID
            assistant_id: 助手 ID
            user_id: 用户 ID
            model_name: 模型名称
            status: 运行状态
            multitask_strategy: 多任务策略
            metadata: 元数据
            kwargs: 关键字参数
            error: 错误信息
            created_at: 创建时间
        """
        now = datetime.now(UTC).isoformat()
        self._runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "user_id": user_id,
            "model_name": model_name,
            "status": status,
            "multitask_strategy": multitask_strategy,
            "metadata": metadata or {},
            "kwargs": kwargs or {},
            "error": error,
            "created_at": created_at or now,
            "updated_at": now,
        }

    async def get(self, run_id):
        """获取运行记录。

        Args:
            run_id: 运行 ID

        Returns:
            运行记录字典或 None
        """
        return self._runs.get(run_id)

    async def list_by_thread(self, thread_id, *, user_id=None, limit=100):
        """列出线程的所有运行。

        Args:
            thread_id: 线程 ID
            user_id: 用户 ID 过滤器
            limit: 返回记录数量限制

        Returns:
            运行记录字典列表，按创建时间降序排列
        """
        results = [r for r in self._runs.values() if r["thread_id"] == thread_id and (user_id is None or r.get("user_id") == user_id)]
        results.sort(key=lambda r: r["created_at"], reverse=True)
        return results[:limit]

    async def update_status(self, run_id, status, *, error=None):
        """更新运行状态。

        Args:
            run_id: 运行 ID
            status: 新状态
            error: 可选的错误信息
        """
        if run_id in self._runs:
            self._runs[run_id]["status"] = status
            if error is not None:
                self._runs[run_id]["error"] = error
            self._runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()

    async def delete(self, run_id):
        """删除运行记录。

        Args:
            run_id: 运行 ID
        """
        self._runs.pop(run_id, None)

    async def update_run_completion(self, run_id, *, status, **kwargs):
        """更新运行完成数据。

        Args:
            run_id: 运行 ID
            status: 最终状态
            **kwargs: 完成数据字段
        """
        if run_id in self._runs:
            self._runs[run_id]["status"] = status
            for key, value in kwargs.items():
                if value is not None:
                    self._runs[run_id][key] = value
            self._runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()

    async def list_pending(self, *, before=None):
        """列出待处理的运行。

        Args:
            before: 可选的时间过滤器

        Returns:
            待处理运行字典列表，按创建时间升序排列
        """
        now = before or datetime.now(UTC).isoformat()
        results = [r for r in self._runs.values() if r["status"] == "pending" and r["created_at"] <= now]
        results.sort(key=lambda r: r["created_at"])
        return results

    async def aggregate_tokens_by_thread(self, thread_id: str) -> dict[str, Any]:
        """聚合线程中已完成运行的 token 使用量。

        Args:
            thread_id: 线程 ID

        Returns:
            包含 token 统计的字典
        """
        completed = [r for r in self._runs.values() if r["thread_id"] == thread_id and r.get("status") in ("success", "error")]
        by_model: dict[str, dict] = {}
        for r in completed:
            model = r.get("model_name") or "unknown"
            entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
            entry["tokens"] += r.get("total_tokens", 0)
            entry["runs"] += 1
        return {
            "total_tokens": sum(r.get("total_tokens", 0) for r in completed),
            "total_input_tokens": sum(r.get("total_input_tokens", 0) for r in completed),
            "total_output_tokens": sum(r.get("total_output_tokens", 0) for r in completed),
            "total_runs": len(completed),
            "by_model": by_model,
            "by_caller": {
                "lead_agent": sum(r.get("lead_agent_tokens", 0) for r in completed),
                "subagent": sum(r.get("subagent_tokens", 0) for r in completed),
                "middleware": sum(r.get("middleware_tokens", 0) for r in completed),
            },
        }
