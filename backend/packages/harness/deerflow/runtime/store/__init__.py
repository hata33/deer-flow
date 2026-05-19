"""
DeerFlow 运行时的存储提供者模块。

重新导出异步提供者（用于长期运行的服务器）和同步提供者（用于 CLI 工具
和嵌入式客户端）的公共 API。

异步用法（FastAPI lifespan）::

    from deerflow.runtime.store import make_store

    async with make_store() as store:
        app.state.store = store

同步用法（CLI / DeerFlowClient）::

    from deerflow.runtime.store import get_store, store_context

    store = get_store()                   # 单例
    with store_context() as store: ...    # 一次性
"""

# 从异步提供者模块导入存储创建函数
from .async_provider import make_store

# 从同步提供者模块导入存储管理函数
# - get_store: 获取当前存储实例
# - reset_store: 重置存储
# - store_context: 存储上下文管理器
from .provider import get_store, reset_store, store_context

__all__ = [
    # 异步
    "make_store",       # 创建存储实例（异步上下文管理器）
    # 同步
    "get_store",        # 获取当前存储实例
    "reset_store",      # 重置存储
    "store_context",    # 存储上下文管理器
]
