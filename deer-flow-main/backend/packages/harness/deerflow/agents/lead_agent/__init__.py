"""主智能体（Lead Agent）模块。

提供基于配置的主智能体工厂函数 make_lead_agent，
用于创建带有完整中间件链和工具集的 LangGraph 编译图。
"""

from .agent import make_lead_agent

__all__ = ["make_lead_agent"]
