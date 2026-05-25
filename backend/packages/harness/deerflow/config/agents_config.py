"""自定义代理配置 — 用户定义的代理类型。

自定义代理是用户创建的专门化代理，拥有独立的：
- SOUL.md: 代理的个性和行为指导
- config.yaml: 代理的配置（模型、工具、技能白名单）

### 用户隔离
代理按用户隔离存储：
- 新布局（推荐）: {base_dir}/users/{user_id}/agents/{name}/
- 旧布局（只读兼容）: {base_dir}/agents/{name}/

新写入始终使用新布局。旧布局仅作为读取回退，
直到用户运行 migrate_user_isolation.py 迁移脚本。

### 名称验证
代理名只允许字母、数字和连字符，防止路径遍历攻击。

### 配置字段
- name: 代理名称（从目录名推断，如果 config.yaml 中未指定）
- description: 代理描述
- model: 使用的模型（None = 继承主代理的模型）
- tool_groups: 工具分组白名单
- skills: 技能白名单（None=全部启用, []=全部禁用）
"""

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)

SOUL_FILENAME = "SOUL.md"
# 代理名只允许字母、数字和连字符，防止路径遍历
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


def validate_agent_name(name: str | None) -> str | None:
    """验证代理名称是否安全（只包含字母、数字、连字符）。

    在用于文件系统路径之前调用，防止路径遍历攻击。
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("Invalid agent name. Expected a string or None.")
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")
    return name


class AgentConfig(BaseModel):
    """自定义代理配置模型。

    - name: 代理唯一名称
    - description: 代理描述
    - model: 使用的模型名称（None = 继承主代理模型）
    - tool_groups: 允许的工具分组列表
    - skills: 技能白名单
      - None（或省略）: 加载所有启用的技能（默认行为）
      - []（显式空列表）: 禁用所有技能
      - ["skill1", "skill2"]: 只加载指定技能
    """

    name: str
    description: str = ""
    model: str | None = None
    tool_groups: list[str] | None = None
    skills: list[str] | None = None


def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path:
    """解析代理的磁盘目录，优先使用按用户隔离的布局。

    解析顺序：
    1. {base_dir}/users/{user_id}/agents/{name}/（按用户隔离，当前布局）
    2. {base_dir}/agents/{name}/（旧布局，只读回退）

    如果两者都不存在，返回按用户隔离的路径（供新建代理使用）。

    Args:
        name: 已验证的代理名称。
        user_id: 代理所有者。默认为请求上下文中的有效用户。
    """
    paths = get_paths()
    effective_user = user_id or get_effective_user_id()
    user_path = paths.user_agent_dir(effective_user, name)
    if user_path.exists():
        return user_path

    # 回退到旧布局（兼容未迁移的安装）
    legacy_path = paths.agent_dir(name)
    if legacy_path.exists():
        return legacy_path

    # 都不存在时返回新布局路径（用于创建新代理）
    return user_path


def load_agent_config(name: str | None, *, user_id: str | None = None) -> AgentConfig | None:
    """从磁盘加载自定义代理的配置。

    先查找按用户隔离的布局，回退到旧布局。
    config.yaml 中缺失的字段用默认值填充。

    Raises:
        FileNotFoundError: 代理目录或 config.yaml 不存在
        ValueError: config.yaml 解析失败
    """
    if name is None:
        return None

    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
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

    # 如果 config.yaml 中没有 name 字段，用目录名填充
    if "name" not in data:
        data["name"] = name

    # 过滤掉 Pydantic 模型不认识的字段（如旧版的 prompt_file）
    known_fields = set(AgentConfig.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}

    return AgentConfig(**data)


def load_agent_soul(agent_name: str | None, *, user_id: str | None = None) -> str | None:
    """读取代理的 SOUL.md 文件。

    SOUL.md 定义代理的个性、价值观和行为边界。
    它被注入到主代理的系统提示词中作为额外上下文。

    Returns:
        SOUL.md 内容字符串，或 None（文件不存在或为空）。
    """
    if agent_name:
        agent_dir = resolve_agent_dir(agent_name, user_id=user_id)
    else:
        # 无代理名时使用基础目录（可能存在全局 SOUL.md）
        agent_dir = get_paths().base_dir
    soul_path = agent_dir / SOUL_FILENAME
    if not soul_path.exists():
        return None
    content = soul_path.read_text(encoding="utf-8").strip()
    return content or None


def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]:
    """扫描并返回所有有效的自定义代理。

    合并按用户隔离布局和旧布局中的代理，
    按用户布局的代理会覆盖旧布局中的同名代理。

    Returns:
        按名称排序的 AgentConfig 列表。
    """
    paths = get_paths()
    effective_user = user_id or get_effective_user_id()

    seen: set[str] = set()
    agents: list[AgentConfig] = []

    user_root = paths.user_agents_dir(effective_user)
    legacy_root = paths.agents_dir

    for root in (user_root, legacy_root):
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name in seen:
                continue
            config_file = entry / "config.yaml"
            if not config_file.exists():
                logger.debug(f"Skipping {entry.name}: no config.yaml")
                continue

            try:
                agent_cfg = load_agent_config(entry.name, user_id=effective_user)
                if agent_cfg is None:
                    continue
                agents.append(agent_cfg)
                seen.add(entry.name)
            except Exception as e:
                logger.warning(f"Skipping agent '{entry.name}': {e}")

    agents.sort(key=lambda a: a.name)
    return agents
