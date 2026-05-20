"""
运行元数据存储的抽象接口。

RunManager 依赖此接口。实现:
- MemoryRunStore: 内存字典（开发、测试）
- 未来: 由 SQLAlchemy ORM 支持的 RunRepository

所有方法都接受可选的 user_id 用于用户隔离。
当 user_id 为 None 时，不应用用户过滤（单用户模式）。
"""

from __future__ import annotations

import abc
from typing import Any


class RunStore(abc.ABC):
    """运行元数据存储接口。

    定义了运行记录的持久化操作抽象方法。
    """

    @abc.abstractmethod
    async def put(
        self,
        run_id: str,
        *,
        thread_id: str,
        assistant_id: str | None = None,
        user_id: str | None = None,
        model_name: str | None = None,
        status: str = "pending",
        multitask_strategy: str = "reject",
        metadata: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> None:
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

    @abc.abstractmethod
    async def get(
        self,
        run_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        pass

    @abc.abstractmethod
    async def list_by_thread(
        self,
        thread_id: str,
        *,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出线程的所有运行。

        Args:
            thread_id: 线程 ID
            user_id: 用户 ID 过滤器
            limit: 返回记录数量限制

        Returns:
            运行记录字典列表
        """

    @abc.abstractmethod
    async def update_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        """更新运行状态。

        Args:
            run_id: 运行 ID
            status: 新状态
            error: 可选的错误信息
        """

    @abc.abstractmethod
    async def delete(self, run_id: str) -> None:
        """删除运行记录。

        Args:
            run_id: 运行 ID
        """

    @abc.abstractmethod
    async def update_model_name(
        self,
        run_id: str,
        model_name: str | None,
    ) -> None:
        """Update the model_name field for an existing run."""
        pass

    @abc.abstractmethod
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
        """更新运行完成数据。

        Args:
            run_id: 运行 ID
            status: 最终状态
            total_input_tokens: 总输入 token 数
            total_output_tokens: 总输出 token 数
            total_tokens: 总 token 数
            llm_call_count: LLM 调用次数
            lead_agent_tokens: lead agent token 数
            subagent_tokens: subagent token 数
            middleware_tokens: middleware token 数
            message_count: 消息数量
            last_ai_message: 最后一条 AI 消息
            first_human_message: 第一条 human 消息
            error: 可选的错误信息
        """

    @abc.abstractmethod
    async def list_pending(self, *, before: str | None = None) -> list[dict[str, Any]]:
        """列出待处理的运行。

        Args:
            before: 可选的时间过滤器

        Returns:
            待处理运行字典列表
        """

    @abc.abstractmethod
    async def aggregate_tokens_by_thread(self, thread_id: str) -> dict[str, Any]:
        """聚合线程中已完成运行的 token 使用量。

        Args:
            thread_id: 线程 ID

        Returns:
            包含以下键的字典:
            - total_tokens: 总 token 数
            - total_input_tokens: 总输入 token 数
            - total_output_tokens: 总输出 token 数
            - total_runs: 总运行数
            - by_model: 按模型分组的统计 (model_name → {tokens, runs})
            - by_caller: 按调用者分组的统计 ({lead_agent, subagent, middleware})
        """
        pass
