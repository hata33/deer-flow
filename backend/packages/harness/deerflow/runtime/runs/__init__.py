"""
运行生命周期管理模块，用于 LangGraph Platform API 兼容性。

提供运行管理、执行上下文和状态跟踪功能。
"""

# 从管理器模块导入运行管理相关类和异常
# - ConflictError: 冲突错误异常
# - RunManager: 运行管理器
# - RunRecord: 运行记录
# - UnsupportedStrategyError: 不支持的策略错误异常
from .manager import ConflictError, RunManager, RunRecord, UnsupportedStrategyError

# 从模式模块导入枚举类型
# - DisconnectMode: 断开连接模式枚举
# - RunStatus: 运行状态枚举
from .schemas import DisconnectMode, RunStatus

# 从工作器模块导入运行上下文和主函数
# - RunContext: 运行上下文
# - run_agent: 运行 agent 的主函数
from .worker import RunContext, run_agent

__all__ = [
    "ConflictError",           # 冲突错误异常
    "DisconnectMode",          # 断开连接模式枚举
    "RunContext",              # 运行上下文
    "RunManager",              # 运行管理器
    "RunRecord",               # 运行记录
    "RunStatus",               # 运行状态枚举
    "UnsupportedStrategyError", # 不支持的策略错误异常
    "run_agent",               # 运行 agent 的主函数
]
