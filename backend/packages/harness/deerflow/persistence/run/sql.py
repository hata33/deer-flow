"""基于 SQLAlchemy 的 RunStore 实现。

每个方法获取并释放自己的短生命周期会话。
运行状态更新来自可能运行数分钟的后台工作线程，
因此不能跨长时间执行持有连接。

本仓库实现了 RunStore 抽象接口，提供了所有运行元数据的 CRUD 操作，
以及 Token 用量聚合查询。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.run.model import RunRow
from deerflow.runtime.runs.store.base import RunStore
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id


class RunRepository(RunStore):
    """运行元数据仓库，实现 RunStore 接口。

    通过 async_sessionmaker 创建短生命周期会话，
    确保后台工作线程不会长时间占用数据库连接。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str | None:
        """规范化模型名称：去除首尾空白，截断到 128 字符。

        作用：防止模型名称过长导致数据库写入失败，
        同时清理可能的空白字符。
        """
        if model_name is None:
            return None
        if not isinstance(model_name, str):
            model_name = str(model_name)
        normalized = model_name.strip()
        if len(normalized) > 128:
            normalized = normalized[:128]
        return normalized

    @staticmethod
    def _safe_json(obj: Any) -> Any:
        """确保对象可 JSON 序列化，回退到 model_dump() 或 str()。

        处理多种输入类型:
          - 基本类型（str, int, float, bool）：直接返回
          - dict/list：递归处理每个元素
          - Pydantic 模型：调用 model_dump() 或 dict()
          - 其他类型：尝试 json.dumps()，失败则转字符串

        作用：防止不可序列化的对象写入 JSON 列时报错。
        """
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {k: RunRepository._safe_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [RunRepository._safe_json(v) for v in obj]
        # 尝试 Pydantic v2 的 model_dump()
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()
            except Exception:
                pass
        # 尝试 Pydantic v1 的 dict()
        if hasattr(obj, "dict"):
            try:
                return obj.dict()
            except Exception:
                pass
        # 最后尝试直接序列化，不行就转字符串
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    @staticmethod
    def _row_to_dict(row: RunRow) -> dict[str, Any]:
        """将 ORM 行转换为字典，重映射 JSON 列名并处理时间格式。

        重映射逻辑:
          - metadata_json → metadata（与 RunStore 接口保持一致）
          - kwargs_json → kwargs
          - datetime → ISO 字符串（与 MemoryRunStore 格式一致）
        """
        d = row.to_dict()
        # 重映射 JSON 列名以匹配 RunStore 接口约定
        d["metadata"] = d.pop("metadata_json", {})
        d["kwargs"] = d.pop("kwargs_json", {})
        # 将 datetime 转为 ISO 字符串，与内存实现保持一致
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                d[key] = val.isoformat()
        return d

    async def put(
        self,
        run_id,
        *,
        thread_id,
        assistant_id=None,
        user_id: str | None | _AutoSentinel = AUTO,
        model_name: str | None = None,
        status="pending",
        multitask_strategy="reject",
        metadata=None,
        kwargs=None,
        error=None,
        created_at=None,
        follow_up_to_run_id=None,
    ):
        """创建一条运行记录。

        将运行参数和元数据写入数据库，支持解析 created_at 字符串
        为 datetime 对象，便于从 JSON 反序列化时恢复时间戳。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.put")
        now = datetime.now(UTC)
        row = RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=assistant_id,
            user_id=resolved_user_id,
            model_name=self._normalize_model_name(model_name),
            status=status,
            multitask_strategy=multitask_strategy,
            metadata_json=self._safe_json(metadata) or {},
            kwargs_json=self._safe_json(kwargs) or {},
            error=error,
            follow_up_to_run_id=follow_up_to_run_id,
            created_at=datetime.fromisoformat(created_at) if created_at else now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()

    async def get(
        self,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """根据 ID 获取运行记录。包含所有者过滤。"""
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.get")
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                return None
            # 所有者过滤
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return None
            return self._row_to_dict(row)

    async def list_by_thread(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        limit=100,
    ):
        """列出指定线程中的运行记录。

        按创建时间降序排列（最新的在前）。
        支持所有者过滤和分页限制。
        """
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.list_by_thread")
        stmt = select(RunRow).where(RunRow.thread_id == thread_id)
        if resolved_user_id is not None:
            stmt = stmt.where(RunRow.user_id == resolved_user_id)
        stmt = stmt.order_by(RunRow.created_at.desc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_status(self, run_id, status, *, error=None):
        """更新运行状态。可选附带错误信息。

        使用 UPDATE 语句直接更新，不加载完整行，性能更优。
        """
        values: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}
        if error is not None:
            values["error"] = error
        async with self._sf() as session:
            await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(**values))
            await session.commit()

    async def update_model_name(self, run_id, model_name):
        """更新运行使用的模型名称。

        模型名称可能延迟确定（如第一次 LLM 调用时才知道实际使用的模型）。
        """
        async with self._sf() as session:
            await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(model_name=self._normalize_model_name(model_name), updated_at=datetime.now(UTC)))
            await session.commit()

    async def delete(
        self,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
    ):
        """删除一条运行记录。包含所有者过滤。"""
        resolved_user_id = resolve_user_id(user_id, method_name="RunRepository.delete")
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                return
            if resolved_user_id is not None and row.user_id != resolved_user_id:
                return
            await session.delete(row)
            await session.commit()

    async def list_pending(self, *, before=None):
        """列出所有待处理的运行（status=pending）。

        按创建时间升序排列（最早的先处理）。
        可选 before 参数过滤指定时间之前创建的运行。
        用于任务调度器获取待执行的运行队列。
        """
        if before is None:
            before_dt = datetime.now(UTC)
        elif isinstance(before, datetime):
            before_dt = before
        else:
            before_dt = datetime.fromisoformat(before)
        stmt = select(RunRow).where(RunRow.status == "pending", RunRow.created_at <= before_dt).order_by(RunRow.created_at.asc())
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_run_completion(
        self,
        run_id: str,
        *,
        status: str,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_tokens: int = 0,
        llm_call_count: int = 0,
        lead_agent_tokens: int = 0,
        subagent_tokens: int = 0,
        middleware_tokens: int = 0,
        message_count: int = 0,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        error: str | None = None,
    ) -> None:
        """运行完成时更新状态 + Token 用量 + 便利字段。

        这是一次性写入操作，将 RunJournal 在内存中累积的所有统计数据
        批量写入数据库。使用 UPDATE 语句直接更新，无需加载完整行。
        """
        values: dict[str, Any] = {
            "status": status,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "llm_call_count": llm_call_count,
            "lead_agent_tokens": lead_agent_tokens,
            "subagent_tokens": subagent_tokens,
            "middleware_tokens": middleware_tokens,
            "message_count": message_count,
            "updated_at": datetime.now(UTC),
        }
        # 便利字段：截断到 2000 字符防止超长
        if last_ai_message is not None:
            values["last_ai_message"] = last_ai_message[:2000]
        if first_human_message is not None:
            values["first_human_message"] = first_human_message[:2000]
        if error is not None:
            values["error"] = error
        async with self._sf() as session:
            await session.execute(update(RunRow).where(RunRow.run_id == run_id).values(**values))
            await session.commit()

    async def aggregate_tokens_by_thread(self, thread_id: str) -> dict[str, Any]:
        """通过 SQL GROUP BY 聚合线程的 Token 用量。

        在数据库端完成聚合计算，避免加载所有运行记录到应用层。

        返回:
          {
            "total_tokens": 1000,        # 总 Token 数
            "total_input_tokens": 800,   # 总输入 Token
            "total_output_tokens": 200,  # 总输出 Token
            "total_runs": 5,             # 完成的运行总数
            "by_model": {                # 按模型分组的统计
              "gpt-4": {"tokens": 800, "runs": 3},
              "claude-3": {"tokens": 200, "runs": 2}
            },
            "by_caller": {               # 按调用方类型分组
              "lead_agent": 600,
              "subagent": 300,
              "middleware": 100
            }
          }
        """
        # 只统计已完成的运行（success 或 error）
        _completed = RunRow.status.in_(("success", "error"))
        _thread = RunRow.thread_id == thread_id
        # 没有模型名时显示 "unknown"
        model_name = func.coalesce(RunRow.model_name, "unknown")

        stmt = (
            select(
                model_name.label("model"),
                func.count().label("runs"),
                func.coalesce(func.sum(RunRow.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(RunRow.total_input_tokens), 0).label("total_input_tokens"),
                func.coalesce(func.sum(RunRow.total_output_tokens), 0).label("total_output_tokens"),
                func.coalesce(func.sum(RunRow.lead_agent_tokens), 0).label("lead_agent"),
                func.coalesce(func.sum(RunRow.subagent_tokens), 0).label("subagent"),
                func.coalesce(func.sum(RunRow.middleware_tokens), 0).label("middleware"),
            )
            .where(_thread, _completed)
            .group_by(model_name)  # 按模型名分组聚合
        )

        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()

        # 汇总各模型的统计数据
        total_tokens = total_input = total_output = total_runs = 0
        lead_agent = subagent = middleware = 0
        by_model: dict[str, dict] = {}
        for r in rows:
            by_model[r.model] = {"tokens": r.total_tokens, "runs": r.runs}
            total_tokens += r.total_tokens
            total_input += r.total_input_tokens
            total_output += r.total_output_tokens
            total_runs += r.runs
            lead_agent += r.lead_agent
            subagent += r.subagent
            middleware += r.middleware

        return {
            "total_tokens": total_tokens,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_runs": total_runs,
            "by_model": by_model,
            "by_caller": {
                "lead_agent": lead_agent,
                "subagent": subagent,
                "middleware": middleware,
            },
        }
