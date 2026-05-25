"""沙箱中间件 —— 管理 Agent 生命周期中的沙箱实例创建与释放。

本模块实现了 :class:`SandboxMiddleware`，它是一个 Agent 中间件，负责在
Agent 执行的合适时机自动创建和分配沙箱实例。

生命周期管理
~~~~~~~~~~~~~
沙箱中间件支持两种初始化模式：

1. **懒加载模式**（lazy_init=True，默认）：
   - 沙箱不在 Agent 启动时创建
   - 由其他中间件（如 :class:`ToolMiddleware`）在首次工具调用时触发创建
   - 优点：如果 Agent 不需要文件/命令操作，则不会浪费资源

2. **急切模式**（lazy_init=False）：
   - 沙箱在 Agent 首次调用前（before_agent）就创建
   - 确保沙箱在 Agent 开始工作时就已就绪

沙箱复用策略
~~~~~~~~~~~~~
- 沙箱在同一个线程（thread）的多次 Agent 调用之间**被复用**，不会在每次
  Agent 调用后释放。这避免了频繁创建/销毁沙箱的开销。
- 沙箱的最终清理在应用程序关闭时通过 ``SandboxProvider.shutdown()`` 执行。
- SandboxMiddleware 的 ``after_agent`` 方法提供了释放沙箱的逻辑，
  但在当前架构中，release 不会被自动调用，以支持跨轮次复用。

状态传递
~~~~~~~~
中间件通过 Agent 的状态字典传递沙箱信息：

- ``state["sandbox"]["sandbox_id"]`` — 当前分配的沙箱 ID
- ``state["thread_data"]`` — 线程上下文数据

这些状态在工具调用时被读取，用于获取对应的沙箱实例。
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
    """沙箱中间件使用的 Agent 状态模式。

    兼容 ``ThreadState`` 的 schema，包含沙箱和线程数据的可选字段。
    这些字段由中间件在 before_agent 阶段注入。

    Attributes:
        sandbox: 沙箱状态，包含 ``sandbox_id`` 等字段。
        thread_data: 线程上下文数据，包含用户信息、线程 ID 等。
    """

    sandbox: NotRequired[SandboxState | None]
    thread_data: NotRequired[ThreadDataState | None]


class SandboxMiddleware(AgentMiddleware[SandboxMiddlewareState]):
    """为 Agent 自动创建和分配沙箱环境的中间件。

    在 Agent 生命周期中的适当时机获取沙箱实例，并将其 ID 注入到 Agent 状态中，
    使得后续的工具调用可以使用该沙箱进行文件操作和命令执行。

    生命周期管理：

    - **懒加载模式**（lazy_init=True）：沙箱在首次工具调用时获取
    - **急切模式**（lazy_init=False）：沙箱在 Agent 首次调用前获取
    - 沙箱在同一线程的多次 Agent 调用之间被复用
    - 沙箱不会在每次 Agent 调用后释放（避免浪费性重建）
    - 清理在应用程序关闭时通过 SandboxProvider.shutdown() 执行
    """

    state_schema = SandboxMiddlewareState

    def __init__(self, lazy_init: bool = True):
        """初始化沙箱中间件。

        Args:
            lazy_init: 如果为 True，延迟到首次工具调用时获取沙箱（推荐）。
                      如果为 False，在 before_agent 阶段立即获取沙箱。
                      默认为 True 以获得最佳性能。
        """
        super().__init__()
        self._lazy_init = lazy_init

    def _acquire_sandbox(self, thread_id: str) -> str:
        """获取一个沙箱实例。

        通过全局 Provider 单例获取沙箱，并记录日志。

        Args:
            thread_id: 当前线程的标识符。

        Returns:
            获取到的沙箱实例 ID。
        """
        provider = get_sandbox_provider()
        sandbox_id = provider.acquire(thread_id)
        logger.info(f"Acquiring sandbox {sandbox_id}")
        return sandbox_id

    @override
    def before_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        """Agent 调用前的钩子：创建并分配沙箱。

        如果启用了懒加载模式（默认），跳过沙箱获取，交给工具调用时处理。
        如果禁用了懒加载模式，在 Agent 开始工作前就获取沙箱。

        Args:
            state: 当前 Agent 状态。
            runtime: LangGraph 运时，包含上下文信息（如 thread_id）。

        Returns:
            状态更新字典（包含 ``{"sandbox": {"sandbox_id": ...}}``），
            或 None 表示无更新。
        """
        # 懒加载模式：跳过获取，由工具中间件在首次调用时触发
        if self._lazy_init:
            return super().before_agent(state, runtime)

        # 急切模式：在 Agent 调用前就获取沙箱
        if "sandbox" not in state or state["sandbox"] is None:
            thread_id = (runtime.context or {}).get("thread_id")
            if thread_id is None:
                # 没有线程上下文，无法获取沙箱，跳过
                return super().before_agent(state, runtime)
            sandbox_id = self._acquire_sandbox(thread_id)
            logger.info(f"Assigned sandbox {sandbox_id} to thread {thread_id}")
            return {"sandbox": {"sandbox_id": sandbox_id}}
        return super().before_agent(state, runtime)

    @override
    def after_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        """Agent 调用后的钩子：释放沙箱。

        从 Agent 状态或运行时上下文中获取沙箱 ID 并释放。

        注意：在当前架构中，此方法定义了释放逻辑，但 SandboxMiddleware
        不会在每次 Agent 调用后自动释放沙箱，以支持跨轮次复用。
        沙箱的最终清理通过 Provider 的 shutdown() 方法完成。

        Args:
            state: 当前 Agent 状态。
            runtime: LangGraph 运行时。

        Returns:
            None（沙箱释放不产生状态更新）。
        """
        # 优先从 Agent 状态中获取沙箱 ID
        sandbox = state.get("sandbox")
        if sandbox is not None:
            sandbox_id = sandbox["sandbox_id"]
            logger.info(f"Releasing sandbox {sandbox_id}")
            get_sandbox_provider().release(sandbox_id)
            return None

        # 备选：从运行时上下文中获取沙箱 ID
        if (runtime.context or {}).get("sandbox_id") is not None:
            sandbox_id = runtime.context.get("sandbox_id")
            logger.info(f"Releasing sandbox {sandbox_id} from context")
            get_sandbox_provider().release(sandbox_id)
            return None

        # 没有需要释放的沙箱
        return super().after_agent(state, runtime)
