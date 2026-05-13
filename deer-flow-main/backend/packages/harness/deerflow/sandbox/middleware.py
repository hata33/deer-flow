"""沙箱生命周期中间件。

在 Agent 调用前后管理沙箱的获取和释放。
支持两种初始化模式：
- lazy_init=True（默认）：延迟到首次工具调用时获取沙箱
- lazy_init=False：在 before_agent 阶段立即获取沙箱
"""

import logging
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import SandboxState, ThreadDataState
from deerflow.sandbox import get_sandbox_provider

logger = logging.getLogger(__name__)


class SandboxMiddlewareState(AgentState):
    """与 ThreadState 兼容的中间件状态模式。"""

    sandbox: NotRequired[SandboxState | None]
    thread_data: NotRequired[ThreadDataState | None]


class SandboxMiddleware(AgentMiddleware[SandboxMiddlewareState]):
    """沙箱生命周期管理中间件。

    生命周期：
    - lazy_init=True：沙箱延迟到首次工具调用时获取
    - lazy_init=False：在 before_agent 阶段立即获取
    - 沙箱在同一线程的多轮对话中复用
    - 沙箱不在每次调用后释放（避免重复创建）
    - 应用关闭时通过 SandboxProvider.shutdown() 统一清理
    """

    state_schema = SandboxMiddlewareState

    def __init__(self, lazy_init: bool = True):
        """初始化沙箱中间件。

        Args:
            lazy_init: True 时延迟到首次工具调用获取，False 时立即获取。
        """
        super().__init__()
        self._lazy_init = lazy_init

    def _acquire_sandbox(self, thread_id: str) -> str:
        """获取沙箱并返回其 ID。"""
        provider = get_sandbox_provider()
        sandbox_id = provider.acquire(thread_id)
        logger.info(f"Acquiring sandbox {sandbox_id}")
        return sandbox_id

    @override
    def before_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        """Agent 调用前的沙箱初始化（仅在 lazy_init=False 时执行）。"""
        if self._lazy_init:
            return super().before_agent(state, runtime)

        # 立即初始化模式
        if "sandbox" not in state or state["sandbox"] is None:
            thread_id = (runtime.context or {}).get("thread_id")
            if thread_id is None:
                return super().before_agent(state, runtime)
            sandbox_id = self._acquire_sandbox(thread_id)
            logger.info(f"Assigned sandbox {sandbox_id} to thread {thread_id}")
            return {"sandbox": {"sandbox_id": sandbox_id}}
        return super().before_agent(state, runtime)

    @override
    def after_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        """Agent 调用后释放沙箱。"""
        sandbox = state.get("sandbox")
        if sandbox is not None:
            sandbox_id = sandbox["sandbox_id"]
            logger.info(f"Releasing sandbox {sandbox_id}")
            get_sandbox_provider().release(sandbox_id)
            return None

        if (runtime.context or {}).get("sandbox_id") is not None:
            sandbox_id = runtime.context.get("sandbox_id")
            logger.info(f"Releasing sandbox {sandbox_id} from context")
            get_sandbox_provider().release(sandbox_id)
            return None

        return super().after_agent(state, runtime)
