"""代理引导创建工具（Setup Agent Tool）

本模块实现了 `setup_agent` 工具，用于引导式创建新的自定义 DeerFlow 代理。

功能说明：
--------
- 创建新的自定义代理，包括 SOUL.md 和 config.yaml
- 如果指定了 agent_name，在用户级别的 agents/ 目录下创建
- 如果未指定 agent_name，更新默认代理的全局 SOUL.md

代理目录结构：
------------
自定义代理：
    {base_dir}/users/{user_id}/agents/{agent_name}/
        ├── config.yaml    # 代理配置（名称、描述、技能白名单）
        └── SOUL.md        # 代理人格和行为定义

默认代理：
    {base_dir}/
        └── SOUL.md        # 默认代理的人格定义

用户隔离：
--------
自定义代理按用户隔离存储。每个用户的代理存储在独立的目录下，
不同用户之间无法看到或修改对方的代理。

config.yaml 结构：
----------------
    name: my-agent           # 代理名称
    description: "..."        # 一行描述
    skills:                   # 可选：技能白名单
      - skill1
      - skill2

创建流程：
--------
1. 验证 agent_name 格式（validate_agent_name）
2. 解析用户 ID（resolve_runtime_user_id）
3. 创建代理目录（mkdir -p）
4. 写入 config.yaml（仅自定义代理）
5. 写入 SOUL.md
6. 返回 Command 更新状态（created_agent_name）

错误处理：
--------
- 如果创建过程中发生异常且目录是新创建的，会自动清理新目录
- 错误信息通过 ToolMessage 返回给代理

状态更新：
--------
工具返回一个 Command 对象，包含：
- created_agent_name：新创建的代理名称
- messages：成功/失败消息（ToolMessage）
"""

import logging

import yaml
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from deerflow.config.agents_config import validate_agent_name
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)


@tool(parse_docstring=True)
def setup_agent(
    soul: str,
    description: str,
    runtime: Runtime,
    skills: list[str] | None = None,
) -> Command:
    """Setup the custom DeerFlow agent.

    设置自定义 DeerFlow 代理。

    Args:
        soul: 定义代理人格和行为的完整 SOUL.md 内容。
        description: 代理功能的一行描述。
        skills: 可选的技能名称列表。None 表示使用所有启用的技能，空列表表示不使用技能。
    """

    agent_name: str | None = runtime.context.get("agent_name") if runtime.context else None
    agent_dir = None
    is_new_dir = False

    try:
        # 验证代理名称格式
        agent_name = validate_agent_name(agent_name)
        paths = get_paths()
        if agent_name:
            # 自定义代理：在当前用户的桶目录下持久化，
            # 不同用户之间不会看到彼此的代理。
            user_id = resolve_runtime_user_id(runtime)
            agent_dir = paths.user_agent_dir(user_id, agent_name)
        else:
            # 默认代理（无 agent_name）：SOUL.md 位于全局基础目录
            agent_dir = paths.base_dir
        is_new_dir = not agent_dir.exists()
        agent_dir.mkdir(parents=True, exist_ok=True)

        if agent_name:
            # 如果提供了 agent_name，在 agents/ 目录下创建自定义代理
            config_data: dict = {"name": agent_name}
            if description:
                config_data["description"] = description
            if skills is not None:
                config_data["skills"] = skills

            config_file = agent_dir / "config.yaml"
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        # 写入 SOUL.md（自定义代理和默认代理都需要）
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

        # 仅当目录是本次调用新创建时才清理
        if agent_name and is_new_dir and agent_dir is not None and agent_dir.exists():
            shutil.rmtree(agent_dir)
        logger.error(f"[agent_creator] Failed to create agent '{agent_name}': {e}", exc_info=True)
        return Command(update={"messages": [ToolMessage(content=f"Error: {e}", tool_call_id=runtime.tool_call_id)]})
