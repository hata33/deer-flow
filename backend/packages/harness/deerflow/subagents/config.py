"""子代理配置定义模块。

本模块定义了子代理的核心配置数据类 SubagentConfig，以及模型名称解析工具函数。
每个子代理在注册时都需要提供一个 SubagentConfig 实例，描述其名称、用途、工具集、
系统提示词、模型选择和执行限制等关键参数。

配置解析优先级:
    1. config.yaml 中 per-agent 覆盖（timeout_seconds, max_turns, model, skills）
    2. config.yaml 中全局默认值（仅对内置代理生效）
    3. SubagentConfig 自身的默认值

模型继承机制:
    model 字段支持 "inherit" 值，表示子代理继承父代理的模型。解析顺序为:
    config.model != "inherit" → 使用自身值
    parent_model is not None → 使用父代理模型
    app_config → 使用配置文件中的默认模型
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig


@dataclass
class SubagentConfig:
    """子代理配置数据类。

    定义单个子代理的完整配置，包括身份标识、行为描述、工具约束、
    模型选择和执行限制。由 registry.py 在注册和发现阶段使用，
    由 executor.py 在创建代理实例时消费。

    Attributes:
        name: 子代理唯一标识符，用于 task() 工具的 subagent_type 参数。
              例如 "general-purpose"、"bash"。
        description: 子代理的功能描述，用于指导主代理判断何时应该
                     将任务委派给该子代理。会出现在 task 工具的描述文本中。
        system_prompt: 子代理的系统提示词，定义其行为规范和输出格式。
                       None 表示不注入系统提示词。
        tools: 允许使用的工具名称白名单。None 表示继承父代理的全部工具；
               列表形式则仅包含列表中指定的工具。
        disallowed_tools: 禁止使用的工具名称黑名单。这些工具在工具过滤时
                          会被强制移除。默认包含 "task" 以防止子代理嵌套。
        skills: 允许加载的技能名称列表。None 表示继承全部已启用的技能；
                空列表 [] 表示不加载任何技能。
        model: 使用的 LLM 模型。"inherit" 表示继承父代理的模型（默认行为），
               也可指定具体模型名称如 "gpt-4o"。
        max_turns: 最大代理轮次限制。超过此轮次后代理自动停止。
                   默认 50 轮。
        timeout_seconds: 最大执行时间（秒）。超时后子代理被标记为 TIMED_OUT。
                         默认 900 秒（15 分钟）。
    """

    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["task"])
    skills: list[str] | None = None
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900


def _default_model_name(app_config: "AppConfig") -> str:
    """获取配置文件中第一个模型的名称作为默认模型。

    当子代理配置为 "inherit" 但没有父代理模型可继承时，
    回退到配置文件中定义的第一个模型。

    Args:
        app_config: 应用配置对象，包含 models 列表。

    Returns:
        第一个配置模型的名称字符串。

    Raises:
        ValueError: 当配置文件中没有任何模型配置时抛出。
    """
    if not app_config.models:
        raise ValueError("No chat models are configured. Please configure at least one model in config.yaml.")
    return app_config.models[0].name


def resolve_subagent_model_name(config: SubagentConfig, parent_model: str | None, *, app_config: "AppConfig | None" = None) -> str:
    """解析子代理实际使用的模型名称。

    模型名称解析遵循以下优先级:
    1. 如果 config.model 不是 "inherit"，直接使用 config.model
    2. 如果提供了 parent_model（父代理模型名称），继承父代理模型
    3. 如果提供了 app_config，使用配置文件中的默认模型
    4. 以上都不满足时，自动加载 app_config 并使用默认模型

    Args:
        config: 子代理配置对象。
        parent_model: 父代理使用的模型名称，可为 None。
        app_config: 应用配置对象，可为 None（懒加载）。

    Returns:
        解析后的模型名称字符串。
    """
    if config.model != "inherit":
        return config.model

    if parent_model is not None:
        return parent_model

    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()
    return _default_model_name(app_config)
