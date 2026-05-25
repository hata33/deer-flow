"""声明式特性标志与中间件定位装饰器。

纯数据类 + 装饰器，无 I/O、无副作用。

RuntimeFeatures 数据类：
  每个特性字段接受三种值：
  - True：使用内置默认中间件
  - False：禁用该特性
  - AgentMiddleware 实例：使用自定义实现替换内置默认

  特殊字段：
  - summarization / guardrail：无内置默认，只接受 False 或自定义实例
    （因为 SummarizationMiddleware 需要 model 参数，GuardrailMiddleware 需要 provider）

@Next / @Prev 装饰器：
  用于 create_deerflow_agent() 的 extra_middleware 参数中，
  声明中间件在链中的相对位置：
  - @Next(A) → 放在 A 类中间件之后
  - @Prev(A) → 放在 A 类中间件之前
  不能同时使用 @Next 和 @Prev。

依赖关系：
  - factory.py：_assemble_from_features() 读取 RuntimeFeatures 组装中间件链
  - factory.py：_insert_extra() 使用 @Next/@Prev 定位插入额外中间件
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from langchain.agents.middleware import AgentMiddleware


@dataclass
class RuntimeFeatures:
    """Declarative feature flags for ``create_deerflow_agent``.

    Most features accept:
    - ``True``: use the built-in default middleware
    - ``False``: disable
    - An ``AgentMiddleware`` instance: use this custom implementation instead

    ``summarization`` and ``guardrail`` have no built-in default — they only
    accept ``False`` (disable) or an ``AgentMiddleware`` instance (custom).
    """

    sandbox: bool | AgentMiddleware = True
    memory: bool | AgentMiddleware = False
    summarization: Literal[False] | AgentMiddleware = False
    subagent: bool | AgentMiddleware = False
    vision: bool | AgentMiddleware = False
    auto_title: bool | AgentMiddleware = False
    guardrail: Literal[False] | AgentMiddleware = False
    loop_detection: bool | AgentMiddleware = True


# ---------------------------------------------------------------------------
# Middleware positioning decorators
# ---------------------------------------------------------------------------


def Next(anchor: type[AgentMiddleware]):
    """Declare this middleware should be placed after *anchor* in the chain."""
    if not (isinstance(anchor, type) and issubclass(anchor, AgentMiddleware)):
        raise TypeError(
            f"@Next expects an AgentMiddleware subclass, got {anchor!r}")

    def decorator(cls: type[AgentMiddleware]) -> type[AgentMiddleware]:
        cls._next_anchor = anchor  # type: ignore[attr-defined]
        return cls

    return decorator


def Prev(anchor: type[AgentMiddleware]):
    """Declare this middleware should be placed before *anchor* in the chain."""
    if not (isinstance(anchor, type) and issubclass(anchor, AgentMiddleware)):
        raise TypeError(
            f"@Prev expects an AgentMiddleware subclass, got {anchor!r}")

    def decorator(cls: type[AgentMiddleware]) -> type[AgentMiddleware]:
        cls._prev_anchor = anchor  # type: ignore[attr-defined]
        return cls

    return decorator
