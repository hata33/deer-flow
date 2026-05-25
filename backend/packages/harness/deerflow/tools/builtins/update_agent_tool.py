"""代理更新工具（Update Agent Tool）

本模块实现了 `update_agent` 工具，允许自定义代理持久化更新自身的 SOUL.md
和 config.yaml。

绑定条件：
--------
仅当 `runtime.context['agent_name']` 已设置时（即在现有自定义代理的对话中）
才绑定到主代理。默认代理不会看到此工具，引导流程继续使用 `setup_agent`
进行初始创建握手。

写入路径：
--------
工具写回 `{base_dir}/users/{user_id}/agents/{agent_name}/{config.yaml,SOUL.md}`，
因此一个用户创建的代理对其他用户不可见（也不可修改）。

原子写入策略：
------------
写入操作分两个阶段：
1. **暂存阶段**：将所有要重写的文件先写入临时文件（.tmp 后缀）
2. **提交阶段**：所有临时文件写入成功后，使用 Path.replace 原子性重命名

这种两阶段提交确保：
- 部分失败不会留下 config.yaml 已更新但 SOUL.md 仍为旧内容的情况
- POSIX/NTFS 上 Path.replace 对单个文件是原子操作

可更新字段：
----------
- **soul**：SOUL.md 的完整替换内容（无补丁语义，必须从当前内容开始编辑）
- **description**：一行描述
- **skills**：技能白名单（[] = 禁用所有技能，省略 = 保持不变）
- **tool_groups**：工具组白名单
- **model**：模型覆盖（必须匹配 config.yaml 中配置的模型名称）

用户隔离：
--------
使用 `resolve_runtime_user_id` 解析用户 ID，确保更新只影响当前用户的代理。
该函数优先使用 `runtime.context["user_id"]`（由网关从认证请求中设置），
回退到 contextvar，最后使用 DEFAULT_USER_ID。

变更生效时机：
-----------
更新在下一个用户回合生效（当主代理使用新的 SOUL.md 和 config.yaml
重新构建时）。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from deerflow.config.agents_config import load_agent_config, validate_agent_name
from deerflow.config.app_config import get_app_config
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)


def _stage_temp(path: Path, text: str) -> Path:
    """将文本内容写入目标路径的相邻临时文件，返回临时文件路径。

    调用者负责在所有暂存文件就绪后使用 Path.replace 将临时文件
    重命名为目标文件，或在失败时删除临时文件。

    Args:
        path: 最终目标文件路径
        text: 要写入的文本内容

    Returns:
        临时文件路径
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    )
    try:
        fd.write(text)
        fd.flush()
        fd.close()
        return Path(fd.name)
    except BaseException:
        fd.close()
        Path(fd.name).unlink(missing_ok=True)
        raise


def _cleanup_temps(temps: list[Path]) -> None:
    """尽力清理暂存的临时文件。"""
    for tmp in temps:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to clean up temp file %s", tmp, exc_info=True)


@tool(parse_docstring=True)
def update_agent(
    runtime: Runtime,
    soul: str | None = None,
    description: str | None = None,
    skills: list[str] | None = None,
    tool_groups: list[str] | None = None,
    model: str | None = None,
) -> Command:
    """Persist updates to the current custom agent's SOUL.md and config.yaml.

    持久化更新当前自定义代理的 SOUL.md 和 config.yaml。

    当用户要求调整代理的身份、描述、技能白名单、工具组白名单或默认模型时使用。
    只有明确传递的字段会被更新；省略的字段保持现有值。

    传递 ``soul`` 作为完整的替换 SOUL.md 内容——没有补丁语义，
    因此始终从当前 SOUL 开始并应用编辑。

    传递 ``skills=[]`` 可禁用此代理的所有技能。省略 ``skills``
    则保持现有白名单不变。

    Args:
        soul: 可选的完整替换 SOUL.md 内容。
        description: 可选的新一行描述。
        skills: 可选的技能白名单。``[]`` = 无技能，省略 = 不变。
        tool_groups: 可选的工具组白名单。``[]`` = 空，省略 = 不变。
        model: 可选的模型覆盖（必须匹配配置的模型名称）。

    Returns:
        包含描述结果的 ToolMessage 的 Command。更改在下一个用户回合生效
        （当主代理使用新的 SOUL.md 和 config.yaml 重新构建时）。
    """
    tool_call_id = runtime.tool_call_id
    agent_name_raw: str | None = runtime.context.get("agent_name") if runtime.context else None

    def _err(message: str) -> Command:
        """创建错误消息的 Command 辅助函数。"""
        return Command(update={"messages": [ToolMessage(content=f"Error: {message}", tool_call_id=tool_call_id)]})

    # 至少需要一个字段
    if soul is None and description is None and skills is None and tool_groups is None and model is None:
        return _err("No fields provided. Pass at least one of: soul, description, skills, tool_groups, model.")

    # 验证代理名称
    try:
        agent_name = validate_agent_name(agent_name_raw)
    except ValueError as e:
        return _err(str(e))

    if not agent_name:
        return _err("update_agent is only available inside a custom agent's chat. There is no agent_name in the current runtime context, so there is nothing to update. If you are inside the bootstrap flow, use setup_agent instead.")

    # 解析活跃用户，确保更新只影响此用户的代理。
    # resolve_runtime_user_id 优先使用 runtime.context["user_id"]（由
    # 网关从认证请求中设置），然后回退到 contextvar，最后使用 DEFAULT_USER_ID。
    # 这与 setup_agent 匹配，因此创建代理和后续优化始终操作相同的文件，
    # 即使 contextvar 在异步/线程边界间丢失（issue #2782 / #2862 类 bug）。
    user_id = resolve_runtime_user_id(runtime)

    # 在触碰文件系统之前拒绝未知的 model。
    # 否则 _resolve_model_name 在运行时静默回退到默认值，
    # 用户会在每个后续回合看到令人困惑的重复警告。
    if model is not None and get_app_config().get_model_config(model) is None:
        return _err(f"Unknown model '{model}'. Pass a model name that exists in config.yaml's models section.")

    paths = get_paths()
    agent_dir = paths.user_agent_dir(user_id, agent_name)

    # 检查是否存在仅旧版共享布局的代理
    if not agent_dir.exists() and paths.agent_dir(agent_name).exists():
        return _err(f"Agent '{agent_name}' only exists in the legacy shared layout and is not scoped to a user. Run scripts/migrate_user_isolation.py to move legacy agents into the per-user layout before updating.")

    # 加载现有配置
    try:
        existing_cfg = load_agent_config(agent_name, user_id=user_id)
    except FileNotFoundError:
        return _err(f"Agent '{agent_name}' does not exist for the current user. Use setup_agent to create a new agent first.")
    except ValueError as e:
        return _err(f"Agent '{agent_name}' has an unreadable config: {e}")

    if existing_cfg is None:
        return _err(f"Agent '{agent_name}' could not be loaded.")

    updated_fields: list[str] = []

    # 构建更新后的配置数据
    # 强制磁盘上的 name 与我们写入的目录匹配，
    # 即使 existing_cfg.name 已偏移（例如手动编辑 yaml）
    config_data: dict[str, Any] = {"name": agent_name}

    # ── 更新 description ──
    new_description = description if description is not None else existing_cfg.description
    config_data["description"] = new_description
    if description is not None and description != existing_cfg.description:
        updated_fields.append("description")

    # ── 更新 model ──
    new_model = model if model is not None else existing_cfg.model
    if new_model is not None:
        config_data["model"] = new_model
    if model is not None and model != existing_cfg.model:
        updated_fields.append("model")

    # ── 更新 tool_groups ──
    new_tool_groups = tool_groups if tool_groups is not None else existing_cfg.tool_groups
    if new_tool_groups is not None:
        config_data["tool_groups"] = new_tool_groups
    if tool_groups is not None and tool_groups != existing_cfg.tool_groups:
        updated_fields.append("tool_groups")

    # ── 更新 skills ──
    new_skills = skills if skills is not None else existing_cfg.skills
    if new_skills is not None:
        config_data["skills"] = new_skills
    if skills is not None and skills != existing_cfg.skills:
        updated_fields.append("skills")

    config_changed = bool({"description", "model", "tool_groups", "skills"} & set(updated_fields))

    # ── 暂存所有要重写的文件到临时文件 ──
    # 只有在所有临时文件都存在后，才将它们重命名到位——
    # 这样 SOUL.md 上的失败不会留下 config.yaml 已经被替换的情况。
    pending: list[tuple[Path, Path]] = []  # (临时文件, 目标文件)
    staged_temps: list[Path] = []

    try:
        agent_dir.mkdir(parents=True, exist_ok=True)

        if config_changed:
            yaml_text = yaml.dump(config_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
            config_target = agent_dir / "config.yaml"
            config_tmp = _stage_temp(config_target, yaml_text)
            staged_temps.append(config_tmp)
            pending.append((config_tmp, config_target))

        if soul is not None:
            soul_target = agent_dir / "SOUL.md"
            soul_tmp = _stage_temp(soul_target, soul)
            staged_temps.append(soul_tmp)
            pending.append((soul_tmp, soul_target))
            updated_fields.append("soul")

        # ── 提交阶段 ──
        # Path.replace 在 POSIX/NTFS 上对单个文件是原子的，
        # 上面的暂存步骤意味着任何早期失败已经被报告。
        # 剩余的失败模式是两个 replace 调用之间的崩溃，
        # 这通过下面的部分写入错误分支报告，以便调用者知道
        # 哪些文件现在已在磁盘上。
        committed: list[Path] = []
        try:
            for tmp, target in pending:
                tmp.replace(target)
                committed.append(target)
        except Exception as e:
            _cleanup_temps([t for t, _ in pending if t not in committed])
            if committed:
                logger.error(
                    "[update_agent] Partial write for agent '%s' (user=%s): committed=%s, failed during rename: %s",
                    agent_name,
                    user_id,
                    [p.name for p in committed],
                    e,
                    exc_info=True,
                )
                return _err(f"Partial update for agent '{agent_name}': {[p.name for p in committed]} were updated, but the rest failed ({e}). Re-run update_agent to retry the remaining fields.")
            raise

    except Exception as e:
        _cleanup_temps(staged_temps)
        logger.error("[update_agent] Failed to update agent '%s' (user=%s): %s", agent_name, user_id, e, exc_info=True)
        return _err(f"Failed to update agent '{agent_name}': {e}")

    if not updated_fields:
        return Command(update={"messages": [ToolMessage(content=f"No changes applied to agent '{agent_name}'. The provided values matched the existing config.", tool_call_id=tool_call_id)]})

    logger.info("[update_agent] Updated agent '%s' (user=%s) fields: %s", agent_name, user_id, updated_fields)
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=(f"Agent '{agent_name}' updated successfully. Changed: {', '.join(updated_fields)}. The new configuration takes effect on the next user turn."),
                    tool_call_id=tool_call_id,
                )
            ]
        }
    )
