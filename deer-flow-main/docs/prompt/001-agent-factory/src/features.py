"""声明式特性标志与中间件定位装饰器。

为 create_deerflow_agent 提供纯数据类和装饰器，
用于声明式地控制中间件的启用/禁用及在链中的位置。
不涉及 I/O 操作和副作用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain.agents.middleware import AgentMiddleware


@dataclass
class RuntimeFeatures:
    """create_deerflow_agent 的声明式特性标志。

    大多数特性接受三种值：
    - True: 使用内置默认中间件
    - False: 禁用该特性
    - AgentMiddleware 实例: 使用自定义实现替代

    summarization 和 guardrail 没有内置默认值，
    只接受 False（禁用）或 AgentMiddleware 实例（自定义）。
    """

    sandbox: bool | AgentMiddleware = True  # 沙箱环境（默认启用）
    memory: bool | AgentMiddleware = False  # 记忆系统（默认禁用）
    summarization: Literal[False] | AgentMiddleware = False  # 上下文摘要（需自定义实例）
    subagent: bool | AgentMiddleware = False  # 子智能体委托（默认禁用）
    vision: bool | AgentMiddleware = False  # 视觉/图片理解（默认禁用）
    auto_title: bool | AgentMiddleware = False  # 自动标题生成（默认禁用）
    guardrail: Literal[False] | AgentMiddleware = False  # 安全护栏（需自定义实例）


# ---------------------------------------------------------------------------
# 中间件定位装饰器
# ---------------------------------------------------------------------------


def Next(anchor: type[AgentMiddleware]):
    """声明此中间件应放在 anchor 中间件之后。"""
    if not (isinstance(anchor, type) and issubclass(anchor, AgentMiddleware)):
        raise TypeError(f"@Next expects an AgentMiddleware subclass, got {anchor!r}")

    def decorator(cls: type[AgentMiddleware]) -> type[AgentMiddleware]:
        cls._next_anchor = anchor  # type: ignore[attr-defined]
        return cls

    return decorator


def Prev(anchor: type[AgentMiddleware]):
    """声明此中间件应放在 anchor 中间件之前。"""
    if not (isinstance(anchor, type) and issubclass(anchor, AgentMiddleware)):
        raise TypeError(f"@Prev expects an AgentMiddleware subclass, got {anchor!r}")

    def decorator(cls: type[AgentMiddleware]) -> type[AgentMiddleware]:
        cls._prev_anchor = anchor  # type: ignore[attr-defined]
        return cls

    return decorator
