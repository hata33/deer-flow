"""基于技能声明的工具访问策略。

技能可以通过 ``allowed-tools`` frontmatter 字段声明它需要哪些工具。
本模块将多个技能的声明组合成统一的工具过滤策略。

策略规则（按优先级）
  1. 如果**没有任何**已加载的技能声明 ``allowed-tools`` → 返回 ``None``
     （兼容旧行为：所有工具可用）。
  2. 如果**至少有一个**技能声明了 ``allowed-tools`` → 取所有声明的并集。
     未声明该字段的技能在此模式下不贡献任何工具。

设计理由
  这个"一旦有人声明就切到白名单模式"的规则防止了安全降级：
  如果技能 A 声明 ``["read", "write"]`` 而技能 B 没有声明，
  按并集逻辑 B 会把所有工具都加回来，使 A 的限制形同虚设。
  当前的规则避免了这个问题。

Protocol 类型
  ``NamedTool`` 是一个 Protocol（结构化子类型），而非显式的 ABC。
  这意味着任何具有 ``name: str`` 属性的对象都可以被过滤，
  无需在工具类上显式继承或注册。
"""

import logging
from typing import Protocol

from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)


class NamedTool(Protocol):
    """工具的结构化子类型协议。

    任何具有 ``name: str`` 属性的对象都满足此协议，无需显式继承。
    这使得 :func:`filter_tools_by_skill_allowed_tools` 可以作用于
    任何工具实现（LangChain Tool、自定义函数包装器等）。
    """
    name: str


def allowed_tool_names_for_skills(skills: list[Skill]) -> set[str] | None:
    """返回所有已加载技能声明的工具名称并集。

    **返回值语义**：
      - ``None``：不限制 —— 所有工具可用（兼容旧行为）。
        仅当没有技能声明 ``allowed-tools`` 时返回。
      - ``set()``（空集）：所有声明了 ``allowed-tools`` 的技能都显式
        设为空列表 → 没有工具可用。
      - ``{"tool_a", "tool_b"}``：仅这些工具可用。

    **并集逻辑与安全考量**：
      一旦有任意技能声明了 ``allowed-tools``，系统切换到白名单模式。
      未声明的技能不向并集贡献任何工具。
      这避免了安全降级：不能让一个无限制的技能打破其他技能的限制。

    Args:
        skills: 当前会话中已加载的技能列表。

    Returns:
        允许的工具名称集合，或 ``None`` 表示不限制。
    """
    if not skills:
        return None

    allowed: set[str] = set()
    has_explicit_declaration = False
    for skill in skills:
        if skill.allowed_tools is None:
            # 技能未声明 allowed-tools → 在白名单模式下不贡献工具
            continue
        has_explicit_declaration = True
        if not skill.allowed_tools:
            logger.info("Skill %s declared empty allowed-tools", skill.name)
        allowed.update(skill.allowed_tools)

    if not has_explicit_declaration:
        # 没有技能声明限制 → 保持旧行为（全部允许）
        return None
    return allowed


def filter_tools_by_skill_allowed_tools[ToolT: NamedTool](tools: list[ToolT], skills: list[Skill]) -> list[ToolT]:
    """根据技能声明的 allowed-tools 过滤工具列表。

    这是工具过滤的**唯一入口**。调用方（如 Agent 构建器）传入完整
    工具列表和已加载的技能列表，获得过滤后的工具子集。

    **泛型设计**：使用 ``ToolT: NamedTool`` 约束，因此可以作用于
    任何满足 ``NamedTool`` 协议的类型，保持类型安全的同时兼容
    多种工具实现。

    Args:
        tools: 完整的可用工具列表。
        skills: 当前会话中已加载的技能列表（用于收集工具白名单）。

    Returns:
        过滤后的工具列表（如果不限制则返回原列表）。
    """
    allowed = allowed_tool_names_for_skills(skills)
    if allowed is None:
        # 无限制模式：返回全部工具
        return tools

    # 白名单模式：仅保留名称在 allowed 集合中的工具
    return [tool for tool in tools if tool.name in allowed]
