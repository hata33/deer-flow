"""DeerFlow 智能体（Agent）模块。

提供智能体的创建、工厂方法、中间件链、检查点持久化、线程状态管理等功能。
"""

from .checkpointer import get_checkpointer, make_checkpointer, reset_checkpointer
from .factory import create_deerflow_agent
from .features import Next, Prev, RuntimeFeatures
from .lead_agent import make_lead_agent
from .thread_state import SandboxState, ThreadState

__all__ = [
    "create_deerflow_agent",  # 纯参数工厂：从 Python 参数创建 DeerFlow 智能体
    "RuntimeFeatures",  # 声明式特性标志，控制中间件的启用/禁用
    "Next",  # 中间件定位装饰器：放在目标中间件之后
    "Prev",  # 中间件定位装饰器：放在目标中间件之前
    "make_lead_agent",  # 主智能体工厂：基于配置创建 LangGraph 编译图
    "SandboxState",  # 沙箱状态（sandbox_id）
    "ThreadState",  # 线程状态：扩展 AgentState，包含沙箱、标题、待办等
    "get_checkpointer",  # 获取同步检查点单例
    "reset_checkpointer",  # 重置检查点单例（测试用）
    "make_checkpointer",  # 异步上下文管理器：创建检查点
]
