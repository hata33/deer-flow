import logging
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config.paths import Paths, get_paths

logger = logging.getLogger(__name__)


class ThreadDataMiddlewareState(AgentState):
    """与 `ThreadState` 模式兼容。"""

    thread_data: NotRequired[ThreadDataState | None]


class ThreadDataMiddleware(AgentMiddleware[ThreadDataMiddlewareState]):
    """为每次线程执行创建线程数据目录。

    创建以下目录结构：
    - {base_dir}/threads/{thread_id}/user-data/workspace
    - {base_dir}/threads/{thread_id}/user-data/uploads
    - {base_dir}/threads/{thread_id}/user-data/outputs

    生命周期管理：
    - lazy_init=True（默认）：仅计算路径，按需创建目录
    - lazy_init=False：在 before_agent() 中立即创建目录
    """

    state_schema = ThreadDataMiddlewareState

    def __init__(self, base_dir: str | None = None, lazy_init: bool = True):
        """初始化中间件。

        参数：
            base_dir: 线程数据的基目录。默认使用 Paths 解析。
            lazy_init: 如果为 True，延迟目录创建直到需要时。
                      如果为 False，在 before_agent() 中立即创建目录。
                      默认为 True 以优化性能。
        """
        super().__init__()
        self._paths = Paths(base_dir) if base_dir else get_paths()
        self._lazy_init = lazy_init

    def _get_thread_paths(self, thread_id: str) -> dict[str, str]:
        """获取线程数据目录的路径。

        参数：
            thread_id: 线程 ID。

        返回：
            包含 workspace_path、uploads_path 和 outputs_path 的字典。
        """
        return {
            "workspace_path": str(self._paths.sandbox_work_dir(thread_id)),
            "uploads_path": str(self._paths.sandbox_uploads_dir(thread_id)),
            "outputs_path": str(self._paths.sandbox_outputs_dir(thread_id)),
        }

    def _create_thread_directories(self, thread_id: str) -> dict[str, str]:
        """创建线程数据目录。

        参数：
            thread_id: 线程 ID。

        返回：
            包含已创建目录路径的字典。
        """
        self._paths.ensure_thread_dirs(thread_id)
        return self._get_thread_paths(thread_id)

    @override
    def before_agent(self, state: ThreadDataMiddlewareState, runtime: Runtime) -> dict | None:
        context = runtime.context or {}
        thread_id = context.get("thread_id")
        if thread_id is None:
            config = get_config()
            thread_id = config.get("configurable", {}).get("thread_id")

        if thread_id is None:
            raise ValueError("Thread ID is required in runtime context or config.configurable")

        if self._lazy_init:
            # 延迟初始化：仅计算路径，不创建目录
            paths = self._get_thread_paths(thread_id)
        else:
            # 立即初始化：立即创建目录
            paths = self._create_thread_directories(thread_id)
            logger.debug("Created thread data directories for thread %s", thread_id)

        return {
            "thread_data": {
                **paths,
            }
        }
