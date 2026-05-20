"""DeerFlow 应用持久化层（基于 SQLAlchemy 2.0 异步 ORM）。

本模块负责管理 DeerFlow 自身的应用数据——包括运行（Run）元数据、
线程（Thread）所有权、用户（User）信息、反馈（Feedback）等。
它与 LangGraph 的检查点（checkpointer）完全独立，后者负责管理图执行状态。

持久化层核心功能:
  - init_engine:     初始化数据库引擎（支持 memory / sqlite / postgres 三种后端）
  - close_engine:    关闭引擎并释放所有连接
  - get_session_factory: 获取异步会话工厂，供各 Repository 使用
  - get_engine:      获取当前引擎实例（可能为 None）

使用方式:
    from deerflow.persistence import init_engine, close_engine, get_session_factory
"""

# 从引擎模块中导出生命周期管理函数，供外部调用
from deerflow.persistence.engine import close_engine, get_engine, get_session_factory, init_engine

# 定义公开 API 列表，控制 `from deerflow.persistence import *` 的导出范围
__all__ = ["close_engine", "get_engine", "get_session_factory", "init_engine"]
