"""异步→同步桥接工具（Async-to-Sync Bridge）

本模块提供了将异步工具协程包装为同步可调用对象的机制。

为什么需要同步桥接？
------------------
DeerFlow 的某些调用路径运行在同步上下文中（例如嵌入式 DeerFlowClient），
这些同步调用者通过 `tool.func` 同步调用工具。然而许多工具（特别是 MCP 工具
和 ACP 代理工具）的实现是纯异步的（只定义了 `coroutine` 而没有 `func`）。

`make_sync_tool_wrapper()` 解决了这个矛盾：
- 如果事件循环正在运行 → 使用 ThreadPoolExecutor 在新线程中运行 asyncio.run()
- 如果事件循环未运行 → 直接调用 asyncio.run()

事件循环检测策略：
----------------
1. 尝试获取当前运行中的事件循环（asyncio.get_running_loop()）
2. 如果成功（循环正在运行）：
   - 复制当前 contextvars 上下文
   - 提交到 ThreadPoolExecutor 中执行 asyncio.run()
   - 阻塞等待结果（future.result()）
3. 如果失败（循环未运行）：
   - 直接调用 asyncio.run() 在当前线程中运行

RunnableConfig 传递：
-------------------
如果异步函数声明了 `RunnableConfig` 类型的参数，包装器会自动暴露
`config: RunnableConfig` 参数，允许 LangChain 注入运行时配置。
这覆盖了 DeerFlow 当前需要配置感知的工具，如 `invoke_acp_agent`。

线程池配置：
----------
- 最大工作线程数：10
- 线程名前缀："tool-sync"
- 进程退出时自动关闭（atexit 注册）

使用示例：
--------
    from deerflow.tools.sync import make_sync_tool_wrapper

    # 假设 my_async_tool 是一个只定义了 coroutine 的工具
    my_async_tool.func = make_sync_tool_wrapper(
        my_async_tool.coroutine,
        my_async_tool.name
    )
"""

import asyncio
import atexit
import concurrent.futures
import contextvars
import functools
import logging
from collections.abc import Callable
from typing import Any, get_type_hints

from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

# 用于同步工具调用的共享线程池
# 最大 10 个工作线程，线程名前缀为 "tool-sync"
_SYNC_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="tool-sync")

# 注册进程退出时的清理回调（非阻塞关闭）
atexit.register(lambda: _SYNC_TOOL_EXECUTOR.shutdown(wait=False))


def _get_runnable_config_param(func: Callable[..., Any]) -> str | None:
    """检测异步函数中 RunnableConfig 类型的参数名。

    通过 `get_type_hints()` 检查函数的类型注解，返回第一个
    类型为 `RunnableConfig` 的参数名。如果函数是 `functools.partial`
    包装的，则先解包到原始函数。

    Args:
        func: 待检测的异步可调用对象

    Returns:
        RunnableConfig 参数的名称，如果没有则返回 None
    """
    if isinstance(func, functools.partial):
        func = func.func

    try:
        type_hints = get_type_hints(func)
    except Exception:
        return None

    for name, type_ in type_hints.items():
        if type_ is RunnableConfig:
            return name
    return None


def make_sync_tool_wrapper(coro: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """为异步工具协程构建同步包装器。

    该函数创建一个同步可调用对象，内部处理异步→同步的桥接逻辑。
    它是 `_ensure_sync_invocable_tool()` 的核心实现。

    Args:
        coro: 异步可调用对象（LangChain 工具的 coroutine）
        tool_name: 工具名称，用于错误日志

    Returns:
        适合用作 `BaseTool.func` 的同步可调用对象

    注意事项：
    --------
    - 如果 `coro` 声明了 `RunnableConfig` 参数，包装器会暴露
      `config: RunnableConfig` 参数，以便 LangChain 注入运行时配置。
    - 包装器有意不合成动态函数签名。未来如果某个异步工具同时有
      一个名为 `config` 的用户参数和一个名为其他名字的 `RunnableConfig`
      参数，可能会与 LangChain 注入的 `config` 参数冲突。
      在使用那种签名之前，需要重命名用户参数或扩展此辅助函数。
    """
    # 检测是否需要传递 RunnableConfig
    config_param = _get_runnable_config_param(coro)

    def run_coroutine(*args: Any, **kwargs: Any) -> Any:
        """执行异步协程的核心同步包装。

        事件循环检测策略：
        - 循环正在运行 → 在 ThreadPoolExecutor 中执行 asyncio.run()
        - 循环未运行 → 直接调用 asyncio.run()
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop is not None and loop.is_running():
                # 事件循环已在运行：复制上下文到新线程执行
                # 使用 contextvars.copy_context() 确保上下文变量
                # （如 deferred_tool_registry）能正确传递到工作线程
                context = contextvars.copy_context()
                future = _SYNC_TOOL_EXECUTOR.submit(context.run, lambda: asyncio.run(coro(*args, **kwargs)))
                return future.result()
            # 事件循环未运行：直接在当前线程中执行
            return asyncio.run(coro(*args, **kwargs))
        except Exception as e:
            logger.error("Error invoking tool %r via sync wrapper: %s", tool_name, e, exc_info=True)
            raise

    if config_param:
        # 带 RunnableConfig 参数的包装器
        # LangChain 会将运行时配置注入到 `config` 参数中
        def sync_wrapper(*args: Any, config: RunnableConfig = None, **kwargs: Any) -> Any:
            if config is not None or config_param not in kwargs:
                kwargs[config_param] = config
            return run_coroutine(*args, **kwargs)

        return sync_wrapper

    # 不带 RunnableConfig 参数的简单包装器
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return run_coroutine(*args, **kwargs)

    return sync_wrapper
