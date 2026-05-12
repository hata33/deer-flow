"""Agent 动态创建工具。

通过提供 SOUL.md 内容和描述信息，在运行时动态创建自定义 agent。
创建的 agent 目录包含 config.yaml（元信息）和 SOUL.md（人格定义）。
"""

import logging

import yaml
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from deerflow.config.paths import get_paths

logger = logging.getLogger(__name__)


@tool
def setup_agent(
    soul: str,
    description: str,
    runtime: ToolRuntime,
) -> Command:
    """Setup the custom DeerFlow agent.

    Args:
        soul: Full SOUL.md content defining the agent's personality and behavior.
        description: One-line description of what the agent does.
    """

    agent_name: str | None = runtime.context.get("agent_name") if runtime.context else None

    try:
        paths = get_paths()
        # 根据 agent_name 确定创建目录（具名 agent 在 agents/ 子目录，否则在根目录）
        agent_dir = paths.agent_dir(agent_name) if agent_name else paths.base_dir
        agent_dir.mkdir(parents=True, exist_ok=True)

        if agent_name:
            # 具名 agent：在 agents/ 目录下创建，写入 config.yaml 元信息
            config_data: dict = {"name": agent_name}
            if description:
                config_data["description"] = description

            config_file = agent_dir / "config.yaml"
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        # 写入 SOUL.md 人格定义文件
        soul_file = agent_dir / "SOUL.md"
        soul_file.write_text(soul, encoding="utf-8")

        logger.info(f"[agent_creator] Created agent '{agent_name}' at {agent_dir}")
        return Command(
            update={
                "created_agent_name": agent_name,
                "messages": [ToolMessage(content=f"Agent '{agent_name}' created successfully!", tool_call_id=runtime.tool_call_id)],
            }
        )

    except Exception as e:
        import shutil

        if agent_name and agent_dir.exists():
            # 创建失败时清理已创建的目录（仅清理具名 agent 的目录）
            shutil.rmtree(agent_dir)
        logger.error(f"[agent_creator] Failed to create agent '{agent_name}': {e}", exc_info=True)
        return Command(update={"messages": [ToolMessage(content=f"Error: {e}", tool_call_id=runtime.tool_call_id)]})
