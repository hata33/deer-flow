"""自定义智能体配置加载器。

本模块负责从文件系统加载和管理用户自定义的智能体（Agent）配置。

目录结构约定：
    每个自定义智能体位于 ``{base_dir}/agents/{agent_name}/`` 目录下：
    ```
    agents/
    └── {agent_name}/
        ├── config.yaml    # 智能体配置文件（必需）
        ├── SOUL.md        # 智能体人格/价值观定义（可选）
        └── memory.json    # 智能体专属记忆（可选）
    ```

核心功能：
    - **load_agent_config()** — 从指定智能体目录加载 config.yaml
    - **load_agent_soul()** — 读取 SOUL.md 文件（定义智能体的人格和行为准则）
    - **list_custom_agents()** — 扫描 agents/ 目录，列出所有有效的自定义智能体

配置文件格式（config.yaml）：
    ```yaml
    name: my-agent          # 智能体名称（若省略则取目录名）
    description: "描述"     # 智能体功能描述
    model: openai-gpt4o     # 可选，指定使用的模型
    tool_groups:            # 可选，指定工具组
      - default
      - search
    ```

SOUL.md 说明：
    SOUL.md 定义智能体的人格、价值观和行为准则。
    其内容会被注入到 Lead Agent 的系统提示词中作为额外上下文，
    影响智能体的回复风格和行为方式。
"""
import logging
import re
from typing import Any

import yaml
from pydantic import BaseModel

from deerflow.config.paths import get_paths

logger = logging.getLogger(__name__)

# SOUL.md 文件名（定义智能体的人格/价值观）
SOUL_FILENAME = "SOUL.md"

# 智能体名称合法模式：仅允许字母、数字、连字符
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


class AgentConfig(BaseModel):
    """自定义智能体配置。

    对应 ``{agent_dir}/config.yaml`` 文件的内容。

    Attributes:
        name: 智能体名称。若配置文件中未指定，则取目录名。
        description: 智能体功能描述。
        model: 可选的模型标识名，指定该智能体使用的 LLM 模型。
        tool_groups: 可选的工具组列表，限定该智能体可用的工具集合。
    """

    name: str
    description: str = ""
    model: str | None = None
    tool_groups: list[str] | None = None


def load_agent_config(name: str | None) -> AgentConfig | None:
    """加载指定自定义智能体的配置文件。

    从 ``{base_dir}/agents/{name}/config.yaml`` 读取配置。
    会自动过滤掉 Pydantic 模型不认识的遗留字段（如旧的 prompt_file）。

    Args:
        name: 智能体名称。为 None 时返回 None。

    Returns:
        解析后的 AgentConfig 实例。

    Raises:
        FileNotFoundError: 智能体目录或 config.yaml 不存在。
        ValueError: 智能体名称不合法或 config.yaml 解析失败。
    """

    if name is None:
        return None

    # 校验名称格式，防止路径注入
    if not AGENT_NAME_PATTERN.match(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")
    agent_dir = get_paths().agent_dir(name)
    config_file = agent_dir / "config.yaml"

    if not agent_dir.exists():
        raise FileNotFoundError(f"Agent directory not found: {agent_dir}")

    if not config_file.exists():
        raise FileNotFoundError(f"Agent config not found: {config_file}")

    try:
        with open(config_file, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse agent config {config_file}: {e}") from e

    # 如果配置文件中未指定 name，则使用目录名
    if "name" not in data:
        data["name"] = name

    # 过滤掉 Pydantic 模型不认识的遗留字段（如旧的 prompt_file）
    known_fields = set(AgentConfig.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}

    return AgentConfig(**data)


def load_agent_soul(agent_name: str | None) -> str | None:
    """读取智能体的 SOUL.md 文件内容。

    SOUL.md 定义智能体的人格、价值观和行为准则。
    其内容会被注入到 Lead Agent 的系统提示词中作为额外上下文。

    如果 agent_name 为 None，则在 base_dir 根目录下查找 SOUL.md
    （作为默认智能体的人格定义）。

    Args:
        agent_name: 智能体名称。None 表示查找默认位置的 SOUL.md。

    Returns:
        SOUL.md 的文本内容，如果文件不存在或为空则返回 None。
    """
    agent_dir = get_paths().agent_dir(agent_name) if agent_name else get_paths().base_dir
    soul_path = agent_dir / SOUL_FILENAME
    if not soul_path.exists():
        return None
    content = soul_path.read_text(encoding="utf-8").strip()
    return content or None


def list_custom_agents() -> list[AgentConfig]:
    """扫描 agents/ 目录，列出所有有效的自定义智能体。

    遍历 ``{base_dir}/agents/`` 下的所有子目录，
    对包含 config.yaml 的目录尝试加载配置。
    无效的智能体会被跳过并记录警告日志。

    Returns:
        所有有效自定义智能体的 AgentConfig 列表，按目录名排序。
    """
    agents_dir = get_paths().agents_dir

    if not agents_dir.exists():
        return []

    agents: list[AgentConfig] = []

    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir():
            continue

        config_file = entry / "config.yaml"
        if not config_file.exists():
            logger.debug(f"Skipping {entry.name}: no config.yaml")
            continue

        try:
            agent_cfg = load_agent_config(entry.name)
            agents.append(agent_cfg)
        except Exception as e:
            logger.warning(f"Skipping agent '{entry.name}': {e}")

    return agents
