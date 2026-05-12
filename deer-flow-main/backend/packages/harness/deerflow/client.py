"""DeerFlowClient — DeerFlow 智能体系统的嵌入式 Python 客户端。

提供对 DeerFlow 智能体能力的直接编程访问，
无需启动 LangGraph Server 或 Gateway API 进程。

用法:
    from deerflow.client import DeerFlowClient

    client = DeerFlowClient()
    response = client.chat("Analyze this paper for me", thread_id="my-thread")
    print(response)

    # 流式调用
    for event in client.stream("hello"):
        print(event)
"""

import asyncio
import json
import logging
import mimetypes
import shutil
import tempfile
import uuid
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# LangChain 智能体相关导入
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

# DeerFlow 内部模块导入
from deerflow.agents.lead_agent.agent import _build_middlewares
from deerflow.agents.lead_agent.prompt import apply_prompt_template
from deerflow.agents.thread_state import ThreadState
from deerflow.config.agents_config import AGENT_NAME_PATTERN
from deerflow.config.app_config import get_app_config, reload_app_config
from deerflow.config.extensions_config import ExtensionsConfig, SkillStateConfig, get_extensions_config, reload_extensions_config
from deerflow.config.paths import get_paths
from deerflow.models import create_chat_model
from deerflow.skills.installer import install_skill_from_archive
from deerflow.uploads.manager import (
    claim_unique_filename,
    delete_file_safe,
    enrich_file_listing,
    ensure_uploads_dir,
    get_uploads_dir,
    list_files_in_dir,
    upload_artifact_url,
    upload_virtual_path,
)

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """流式响应中的单个事件。

    事件类型与 LangGraph SSE 协议保持一致：
        - ``"values"``: 完整状态快照（标题、消息、产物）。
        - ``"messages-tuple"``: 逐消息更新（AI 文本、工具调用、工具结果）。
        - ``"end"``: 流结束。

    Attributes:
        type: 事件类型。
        data: 事件负载数据，内容因类型而异。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


class DeerFlowClient:
    """DeerFlow 智能体系统的嵌入式 Python 客户端。

    提供对 DeerFlow 智能体能力的直接编程访问，
    无需启动 LangGraph Server 或 Gateway API 进程。

    Note:
        多轮对话需要提供 ``checkpointer``。未提供时，每次 ``stream()`` / ``chat()``
        调用都是无状态的——``thread_id`` 仅用于文件隔离（上传 / 产物）。

        系统提示（包括日期、记忆和技能上下文）在内部智能体首次创建时生成，
        并缓存到配置键发生变化为止。可调用 :meth:`reset_agent` 在长期运行的
        进程中强制刷新。

    Example::

        from deerflow.client import DeerFlowClient

        client = DeerFlowClient()

        # 简单一次性调用
        print(client.chat("hello"))

        # 流式调用
        for event in client.stream("hello"):
            print(event.type, event.data)

        # 配置查询
        print(client.list_models())
        print(client.list_skills())
    """

    def __init__(
        self,
        config_path: str | None = None,
        checkpointer=None,
        *,
        model_name: str | None = None,
        thinking_enabled: bool = True,
        subagent_enabled: bool = False,
        plan_mode: bool = False,
        agent_name: str | None = None,
        middlewares: Sequence[AgentMiddleware] | None = None,
    ):
        """初始化客户端。

        加载配置但延迟智能体创建到首次使用时。

        Args:
            config_path: config.yaml 路径，为 None 时使用默认解析。
            checkpointer: LangGraph checkpointer 实例，用于状态持久化。
                在同一 thread_id 上进行多轮对话时必须提供。
                不提供时每次调用都是无状态的。
            model_name: 覆盖配置中的默认模型名称。
            thinking_enabled: 启用模型的扩展思考能力。
            subagent_enabled: 启用子智能体委派。
            plan_mode: 启用 TodoList 中间件（计划模式）。
            agent_name: 要使用的智能体名称。
            middlewares: 可选的自定义中间件列表，注入到智能体中。
        """
        if config_path is not None:
            reload_app_config(config_path)
        self._app_config = get_app_config()

        if agent_name is not None and not AGENT_NAME_PATTERN.match(agent_name):
            raise ValueError(f"Invalid agent name '{agent_name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")

        self._checkpointer = checkpointer
        self._model_name = model_name
        self._thinking_enabled = thinking_enabled
        self._subagent_enabled = subagent_enabled
        self._plan_mode = plan_mode
        self._agent_name = agent_name
        self._middlewares = list(middlewares) if middlewares else []

        # 延迟初始化智能体——首次调用时创建，配置变更时重建
        self._agent = None
        self._agent_config_key: tuple | None = None

    def reset_agent(self) -> None:
        """强制在下次调用时重新创建内部智能体。

        在外部变更（如记忆更新、技能安装）后使用，
        以确保系统提示或工具集反映最新状态。
        """
        self._agent = None
        self._agent_config_key = None

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: Path, data: dict) -> None:
        """原子性地将 JSON 写入 *path*（临时文件 + 替换）。"""
        fd = tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(data, fd, indent=2)
            fd.close()
            Path(fd.name).replace(path)
        except BaseException:
            fd.close()
            Path(fd.name).unlink(missing_ok=True)
            raise

    def _get_runnable_config(self, thread_id: str, **overrides) -> RunnableConfig:
        """构建用于智能体调用的 RunnableConfig。"""
        configurable = {
            "thread_id": thread_id,
            "model_name": overrides.get("model_name", self._model_name),
            "thinking_enabled": overrides.get("thinking_enabled", self._thinking_enabled),
            "is_plan_mode": overrides.get("plan_mode", self._plan_mode),
            "subagent_enabled": overrides.get("subagent_enabled", self._subagent_enabled),
        }
        return RunnableConfig(
            configurable=configurable,
            recursion_limit=overrides.get("recursion_limit", 100),
        )

    def _ensure_agent(self, config: RunnableConfig):
        """当配置相关参数变化时创建（或重建）智能体。"""
        cfg = config.get("configurable", {})
        key = (
            cfg.get("model_name"),
            cfg.get("thinking_enabled"),
            cfg.get("is_plan_mode"),
            cfg.get("subagent_enabled"),
        )

        if self._agent is not None and self._agent_config_key == key:
            return

        thinking_enabled = cfg.get("thinking_enabled", True)
        model_name = cfg.get("model_name")
        subagent_enabled = cfg.get("subagent_enabled", False)
        max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)

        kwargs: dict[str, Any] = {
            "model": create_chat_model(name=model_name, thinking_enabled=thinking_enabled),
            "tools": self._get_tools(model_name=model_name, subagent_enabled=subagent_enabled),
            "middleware": _build_middlewares(config, model_name=model_name, agent_name=self._agent_name, custom_middlewares=self._middlewares),
            "system_prompt": apply_prompt_template(
                subagent_enabled=subagent_enabled,
                max_concurrent_subagents=max_concurrent_subagents,
                agent_name=self._agent_name,
            ),
            "state_schema": ThreadState,
        }
        checkpointer = self._checkpointer
        if checkpointer is None:
            from deerflow.agents.checkpointer import get_checkpointer

            checkpointer = get_checkpointer()
        if checkpointer is not None:
            kwargs["checkpointer"] = checkpointer

        self._agent = create_agent(**kwargs)
        self._agent_config_key = key
        logger.info("Agent created: agent_name=%s, model=%s, thinking=%s", self._agent_name, model_name, thinking_enabled)

    @staticmethod
    def _get_tools(*, model_name: str | None, subagent_enabled: bool):
        """延迟导入以避免模块级循环依赖。"""
        from deerflow.tools import get_available_tools

        return get_available_tools(model_name=model_name, subagent_enabled=subagent_enabled)

    @staticmethod
    def _serialize_message(msg) -> dict:
        """将 LangChain 消息序列化为普通字典，用于 values 事件。"""
        if isinstance(msg, AIMessage):
            d: dict[str, Any] = {"type": "ai", "content": msg.content, "id": getattr(msg, "id", None)}
            if msg.tool_calls:
                d["tool_calls"] = [{"name": tc["name"], "args": tc["args"], "id": tc.get("id")} for tc in msg.tool_calls]
            if getattr(msg, "usage_metadata", None):
                d["usage_metadata"] = msg.usage_metadata
            return d
        if isinstance(msg, ToolMessage):
            return {
                "type": "tool",
                "content": DeerFlowClient._extract_text(msg.content),
                "name": getattr(msg, "name", None),
                "tool_call_id": getattr(msg, "tool_call_id", None),
                "id": getattr(msg, "id", None),
            }
        if isinstance(msg, HumanMessage):
            return {"type": "human", "content": msg.content, "id": getattr(msg, "id", None)}
        if isinstance(msg, SystemMessage):
            return {"type": "system", "content": msg.content, "id": getattr(msg, "id", None)}
        return {"type": "unknown", "content": str(msg), "id": getattr(msg, "id", None)}

    @staticmethod
    def _extract_text(content) -> str:
        """从 AIMessage 内容（字符串或块列表）中提取纯文本。

        字符串片段不加分隔符直接拼接，以避免破坏 token/字符增量或分块 JSON 负载。
        基于字典的文本块被视为完整文本块，用换行符连接以保持可读性。
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            if content and all(isinstance(block, str) for block in content):
                chunk_like = len(content) > 1 and all(isinstance(block, str) and len(block) <= 20 and any(ch in block for ch in '{}[]":,') for block in content)
                return "".join(content) if chunk_like else "\n".join(content)

            pieces: list[str] = []
            pending_str_parts: list[str] = []

            def flush_pending_str_parts() -> None:
                if pending_str_parts:
                    pieces.append("".join(pending_str_parts))
                    pending_str_parts.clear()

            for block in content:
                if isinstance(block, str):
                    pending_str_parts.append(block)
                elif isinstance(block, dict):
                    flush_pending_str_parts()
                    text_val = block.get("text")
                    if isinstance(text_val, str):
                        pieces.append(text_val)

            flush_pending_str_parts()
            return "\n".join(pieces) if pieces else ""
        return str(content)

    # ------------------------------------------------------------------
    # 公共 API — 对话
    # ------------------------------------------------------------------

    def stream(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        **kwargs,
    ) -> Generator[StreamEvent, None, None]:
        """流式执行一轮对话，逐步产生事件。

        每次调用发送一条用户消息并持续产生事件，直到智能体完成本轮。
        必须在初始化时提供 ``checkpointer`` 才能在多次调用间保留多轮上下文。

        事件类型与 LangGraph SSE 协议保持一致，消费者可在 HTTP 流式和
        嵌入模式之间切换，无需更改事件处理逻辑。

        Args:
            message: 用户消息文本。
            thread_id: 对话上下文的线程 ID，为 None 时自动生成。
            **kwargs: 覆盖客户端默认值（model_name、thinking_enabled、
                plan_mode、subagent_enabled、recursion_limit）。

        Yields:
            StreamEvent，包含以下之一：
            - type="values"          data={"title": str|None, "messages": [...], "artifacts": [...]}
            - type="messages-tuple"  data={"type": "ai", "content": str, "id": str}
            - type="messages-tuple"  data={"type": "ai", "content": str, "id": str, "usage_metadata": {...}}
            - type="messages-tuple"  data={"type": "ai", "content": "", "id": str, "tool_calls": [...]}
            - type="messages-tuple"  data={"type": "tool", "content": str, "name": str, "tool_call_id": str, "id": str}
            - type="end"             data={"usage": {"input_tokens": int, "output_tokens": int, "total_tokens": int}}
        """
        if thread_id is None:
            thread_id = str(uuid.uuid4())

        config = self._get_runnable_config(thread_id, **kwargs)
        self._ensure_agent(config)

        state: dict[str, Any] = {"messages": [HumanMessage(content=message)]}
        context = {"thread_id": thread_id}
        if self._agent_name:
            context["agent_name"] = self._agent_name

        seen_ids: set[str] = set()  # 已见消息 ID 集合，用于去重
        cumulative_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}  # 累计 token 用量

        for chunk in self._agent.stream(state, config=config, context=context, stream_mode="values"):
            messages = chunk.get("messages", [])

            for msg in messages:
                msg_id = getattr(msg, "id", None)
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)

                if isinstance(msg, AIMessage):
                    # 从 AI 消息中跟踪 token 用量
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        cumulative_usage["input_tokens"] += usage.get("input_tokens", 0) or 0
                        cumulative_usage["output_tokens"] += usage.get("output_tokens", 0) or 0
                        cumulative_usage["total_tokens"] += usage.get("total_tokens", 0) or 0

                    if msg.tool_calls:
                        yield StreamEvent(
                            type="messages-tuple",
                            data={
                                "type": "ai",
                                "content": "",
                                "id": msg_id,
                                "tool_calls": [{"name": tc["name"], "args": tc["args"], "id": tc.get("id")} for tc in msg.tool_calls],
                            },
                        )

                    text = self._extract_text(msg.content)
                    if text:
                        event_data: dict[str, Any] = {"type": "ai", "content": text, "id": msg_id}
                        if usage:
                            event_data["usage_metadata"] = {
                                "input_tokens": usage.get("input_tokens", 0) or 0,
                                "output_tokens": usage.get("output_tokens", 0) or 0,
                                "total_tokens": usage.get("total_tokens", 0) or 0,
                            }
                        yield StreamEvent(type="messages-tuple", data=event_data)

                elif isinstance(msg, ToolMessage):
                    yield StreamEvent(
                        type="messages-tuple",
                        data={
                            "type": "tool",
                            "content": self._extract_text(msg.content),
                            "name": getattr(msg, "name", None),
                            "tool_call_id": getattr(msg, "tool_call_id", None),
                            "id": msg_id,
                        },
                    )

            # 每个状态快照产生一个 values 事件
            yield StreamEvent(
                type="values",
                data={
                    "title": chunk.get("title"),
                    "messages": [self._serialize_message(m) for m in messages],
                    "artifacts": chunk.get("artifacts", []),
                },
            )

        yield StreamEvent(type="end", data={"usage": cumulative_usage})

    def chat(self, message: str, *, thread_id: str | None = None, **kwargs) -> str:
        """发送消息并返回最终文本响应。

        基于 :meth:`stream` 的便捷封装，仅返回 ``messages-tuple`` 事件中
        **最后一条** AI 文本。如果智能体在一轮中产生多个文本段，
        中间段会被丢弃。使用 :meth:`stream` 可捕获所有事件。

        Args:
            message: 用户消息文本。
            thread_id: 对话上下文的线程 ID，为 None 时自动生成。
            **kwargs: 覆盖客户端默认值（同 stream()）。

        Returns:
            最后一条 AI 消息文本，若无响应则返回空字符串。
        """
        last_text = ""
        for event in self.stream(message, thread_id=thread_id, **kwargs):
            if event.type == "messages-tuple" and event.data.get("type") == "ai":
                content = event.data.get("content", "")
                if content:
                    last_text = content
        return last_text

    # ------------------------------------------------------------------
    # 公共 API — 配置查询
    # ------------------------------------------------------------------

    def list_models(self) -> dict:
        """列出配置中可用的模型。

        Returns:
            包含 "models" 键的字典，值为模型信息字典列表，
            与 Gateway API ``ModelsListResponse`` 架构一致。
        """
        return {
            "models": [
                {
                    "name": model.name,
                    "model": getattr(model, "model", None),
                    "display_name": getattr(model, "display_name", None),
                    "description": getattr(model, "description", None),
                    "supports_thinking": getattr(model, "supports_thinking", False),
                    "supports_reasoning_effort": getattr(model, "supports_reasoning_effort", False),
                }
                for model in self._app_config.models
            ]
        }

    def list_skills(self, enabled_only: bool = False) -> dict:
        """列出可用的技能。

        Args:
            enabled_only: 若为 True，仅返回已启用的技能。

        Returns:
            包含 "skills" 键的字典，值为技能信息字典列表，
            与 Gateway API ``SkillsListResponse`` 架构一致。
        """
        from deerflow.skills.loader import load_skills

        return {
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "license": s.license,
                    "category": s.category,
                    "enabled": s.enabled,
                }
                for s in load_skills(enabled_only=enabled_only)
            ]
        }

    def get_memory(self) -> dict:
        """获取当前记忆数据。

        Returns:
            记忆数据字典（结构参见 src/agents/memory/updater.py）。
        """
        from deerflow.agents.memory.updater import get_memory_data

        return get_memory_data()

    def export_memory(self) -> dict:
        """导出当前记忆数据，用于备份或迁移。"""
        from deerflow.agents.memory.updater import get_memory_data

        return get_memory_data()

    def import_memory(self, memory_data: dict) -> dict:
        """导入并持久化完整的记忆数据。"""
        from deerflow.agents.memory.updater import import_memory_data

        return import_memory_data(memory_data)

    def get_model(self, name: str) -> dict | None:
        """按名称获取特定模型的配置。

        Args:
            name: 模型名称。

        Returns:
            与 Gateway API ``ModelResponse`` 架构一致的模型信息字典，
            未找到时返回 None。
        """
        model = self._app_config.get_model_config(name)
        if model is None:
            return None
        return {
            "name": model.name,
            "model": getattr(model, "model", None),
            "display_name": getattr(model, "display_name", None),
            "description": getattr(model, "description", None),
            "supports_thinking": getattr(model, "supports_thinking", False),
            "supports_reasoning_effort": getattr(model, "supports_reasoning_effort", False),
        }

    # ------------------------------------------------------------------
    # 公共 API — MCP 配置
    # ------------------------------------------------------------------

    def get_mcp_config(self) -> dict:
        """获取 MCP 服务器配置。

        Returns:
            包含 "mcp_servers" 键的字典，值为服务器名到配置的映射，
            与 Gateway API ``McpConfigResponse`` 架构一致。
        """
        config = get_extensions_config()
        return {"mcp_servers": {name: server.model_dump() for name, server in config.mcp_servers.items()}}

    def update_mcp_config(self, mcp_servers: dict[str, dict]) -> dict:
        """更新 MCP 服务器配置。

        写入 extensions_config.json 并重新加载缓存。

        Args:
            mcp_servers: 服务器名到配置字典的映射。
                每个值应包含 enabled、type、command、args、env、url 等键。

        Returns:
            包含 "mcp_servers" 键的字典，与 Gateway API
            ``McpConfigResponse`` 架构一致。

        Raises:
            OSError: 配置文件无法写入时抛出。
        """
        config_path = ExtensionsConfig.resolve_config_path()
        if config_path is None:
            raise FileNotFoundError("Cannot locate extensions_config.json. Set DEER_FLOW_EXTENSIONS_CONFIG_PATH or ensure it exists in the project root.")

        current_config = get_extensions_config()

        config_data = {
            "mcpServers": mcp_servers,
            "skills": {name: {"enabled": skill.enabled} for name, skill in current_config.skills.items()},
        }

        self._atomic_write_json(config_path, config_data)

        self._agent = None
        self._agent_config_key = None
        reloaded = reload_extensions_config()
        return {"mcp_servers": {name: server.model_dump() for name, server in reloaded.mcp_servers.items()}}

    # ------------------------------------------------------------------
    # 公共 API — 技能管理
    # ------------------------------------------------------------------

    def get_skill(self, name: str) -> dict | None:
        """按名称获取特定技能。

        Args:
            name: 技能名称。

        Returns:
            技能信息字典，未找到时返回 None。
        """
        from deerflow.skills.loader import load_skills

        skill = next((s for s in load_skills(enabled_only=False) if s.name == name), None)
        if skill is None:
            return None
        return {
            "name": skill.name,
            "description": skill.description,
            "license": skill.license,
            "category": skill.category,
            "enabled": skill.enabled,
        }

    def update_skill(self, name: str, *, enabled: bool) -> dict:
        """更新技能的启用状态。

        Args:
            name: 技能名称。
            enabled: 新的启用状态。

        Returns:
            更新后的技能信息字典。

        Raises:
            ValueError: 技能未找到时抛出。
            OSError: 配置文件无法写入时抛出。
        """
        from deerflow.skills.loader import load_skills

        skills = load_skills(enabled_only=False)
        skill = next((s for s in skills if s.name == name), None)
        if skill is None:
            raise ValueError(f"Skill '{name}' not found")

        config_path = ExtensionsConfig.resolve_config_path()
        if config_path is None:
            raise FileNotFoundError("Cannot locate extensions_config.json. Set DEER_FLOW_EXTENSIONS_CONFIG_PATH or ensure it exists in the project root.")

        extensions_config = get_extensions_config()
        extensions_config.skills[name] = SkillStateConfig(enabled=enabled)

        config_data = {
            "mcpServers": {n: s.model_dump() for n, s in extensions_config.mcp_servers.items()},
            "skills": {n: {"enabled": sc.enabled} for n, sc in extensions_config.skills.items()},
        }

        self._atomic_write_json(config_path, config_data)

        self._agent = None
        self._agent_config_key = None
        reload_extensions_config()

        updated = next((s for s in load_skills(enabled_only=False) if s.name == name), None)
        if updated is None:
            raise RuntimeError(f"Skill '{name}' disappeared after update")
        return {
            "name": updated.name,
            "description": updated.description,
            "license": updated.license,
            "category": updated.category,
            "enabled": updated.enabled,
        }

    def install_skill(self, skill_path: str | Path) -> dict:
        """从 .skill 归档文件（ZIP）安装技能。

        Args:
            skill_path: .skill 文件的路径。

        Returns:
            包含 success、skill_name、message 的字典。

        Raises:
            FileNotFoundError: 文件不存在时抛出。
            ValueError: 文件无效时抛出。
        """
        return install_skill_from_archive(skill_path)

    # ------------------------------------------------------------------
    # 公共 API — 记忆管理
    # ------------------------------------------------------------------

    def reload_memory(self) -> dict:
        """从文件重新加载记忆数据，强制缓存失效。

        Returns:
            重新加载后的记忆数据字典。
        """
        from deerflow.agents.memory.updater import reload_memory_data

        return reload_memory_data()

    def clear_memory(self) -> dict:
        """清除所有持久化的记忆数据。"""
        from deerflow.agents.memory.updater import clear_memory_data

        return clear_memory_data()

    def create_memory_fact(self, content: str, category: str = "context", confidence: float = 0.5) -> dict:
        """手动创建单条记忆事实。"""
        from deerflow.agents.memory.updater import create_memory_fact

        return create_memory_fact(content=content, category=category, confidence=confidence)

    def delete_memory_fact(self, fact_id: str) -> dict:
        """按 ID 删除单条记忆事实。"""
        from deerflow.agents.memory.updater import delete_memory_fact

        return delete_memory_fact(fact_id)

    def update_memory_fact(
        self,
        fact_id: str,
        content: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
    ) -> dict:
        """手动更新单条记忆事实，未提供的字段保持不变。"""
        from deerflow.agents.memory.updater import update_memory_fact

        return update_memory_fact(
            fact_id=fact_id,
            content=content,
            category=category,
            confidence=confidence,
        )

    def get_memory_config(self) -> dict:
        """获取记忆系统配置。

        Returns:
            记忆配置字典。
        """
        from deerflow.config.memory_config import get_memory_config

        config = get_memory_config()
        return {
            "enabled": config.enabled,
            "storage_path": config.storage_path,
            "debounce_seconds": config.debounce_seconds,
            "max_facts": config.max_facts,
            "fact_confidence_threshold": config.fact_confidence_threshold,
            "injection_enabled": config.injection_enabled,
            "max_injection_tokens": config.max_injection_tokens,
        }

    def get_memory_status(self) -> dict:
        """获取记忆状态：配置 + 当前数据。

        Returns:
            包含 "config" 和 "data" 键的字典。
        """
        return {
            "config": self.get_memory_config(),
            "data": self.get_memory(),
        }

    # ------------------------------------------------------------------
    # 公共 API — 文件上传
    # ------------------------------------------------------------------

    def upload_files(self, thread_id: str, files: list[str | Path]) -> dict:
        """将本地文件上传到线程的上传目录。

        对于 PDF、PPT、Excel 和 Word 文件，会自动转换为 Markdown。

        Args:
            thread_id: 目标线程 ID。
            files: 本地文件路径列表。

        Returns:
            包含 success、files、message 的字典，与 Gateway API
            ``UploadResponse`` 架构一致。

        Raises:
            FileNotFoundError: 任何文件不存在时抛出。
            ValueError: 任何路径存在但不是普通文件时抛出。
        """
        from deerflow.utils.file_conversion import CONVERTIBLE_EXTENSIONS, convert_file_to_markdown

        # 预先验证所有文件，避免部分上传
        resolved_files = []
        seen_names: set[str] = set()
        has_convertible_file = False
        for f in files:
            p = Path(f)
            if not p.exists():
                raise FileNotFoundError(f"File not found: {f}")
            if not p.is_file():
                raise ValueError(f"Path is not a file: {f}")
            dest_name = claim_unique_filename(p.name, seen_names)
            resolved_files.append((p, dest_name))
            if not has_convertible_file and p.suffix.lower() in CONVERTIBLE_EXTENSIONS:
                has_convertible_file = True

        uploads_dir = ensure_uploads_dir(thread_id)
        uploaded_files: list[dict] = []

        conversion_pool = None
        if has_convertible_file:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                conversion_pool = None
            else:
                import concurrent.futures

                # 在已有事件循环中复用一个工作线程，避免为每个转换文件创建新的线程池
                conversion_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def _convert_in_thread(path: Path):
            return asyncio.run(convert_file_to_markdown(path))

        try:
            for src_path, dest_name in resolved_files:
                dest = uploads_dir / dest_name
                shutil.copy2(src_path, dest)

                info: dict[str, Any] = {
                    "filename": dest_name,
                    "size": str(dest.stat().st_size),
                    "path": str(dest),
                    "virtual_path": upload_virtual_path(dest_name),
                    "artifact_url": upload_artifact_url(thread_id, dest_name),
                }
                if dest_name != src_path.name:
                    info["original_filename"] = src_path.name

                if src_path.suffix.lower() in CONVERTIBLE_EXTENSIONS:
                    try:
                        if conversion_pool is not None:
                            md_path = conversion_pool.submit(_convert_in_thread, dest).result()
                        else:
                            md_path = asyncio.run(convert_file_to_markdown(dest))
                    except Exception:
                        logger.warning(
                            "Failed to convert %s to markdown",
                            src_path.name,
                            exc_info=True,
                        )
                        md_path = None

                    if md_path is not None:
                        info["markdown_file"] = md_path.name
                        info["markdown_path"] = str(uploads_dir / md_path.name)
                        info["markdown_virtual_path"] = upload_virtual_path(md_path.name)
                        info["markdown_artifact_url"] = upload_artifact_url(thread_id, md_path.name)

                uploaded_files.append(info)
        finally:
            if conversion_pool is not None:
                conversion_pool.shutdown(wait=True)

        return {
            "success": True,
            "files": uploaded_files,
            "message": f"Successfully uploaded {len(uploaded_files)} file(s)",
        }

    def list_uploads(self, thread_id: str) -> dict:
        """列出线程上传目录中的文件。

        Args:
            thread_id: 线程 ID。

        Returns:
            包含 "files" 和 "count" 键的字典，与 Gateway API
            ``list_uploaded_files`` 响应一致。
        """
        uploads_dir = get_uploads_dir(thread_id)
        result = list_files_in_dir(uploads_dir)
        return enrich_file_listing(result, thread_id)

    def delete_upload(self, thread_id: str, filename: str) -> dict:
        """删除线程上传目录中的文件。

        Args:
            thread_id: 线程 ID。
            filename: 要删除的文件名。

        Returns:
            包含 success 和 message 的字典，与 Gateway API
            ``delete_uploaded_file`` 响应一致。

        Raises:
            FileNotFoundError: 文件不存在时抛出。
            PermissionError: 检测到路径遍历时抛出。
        """
        from deerflow.utils.file_conversion import CONVERTIBLE_EXTENSIONS

        uploads_dir = get_uploads_dir(thread_id)
        return delete_file_safe(uploads_dir, filename, convertible_extensions=CONVERTIBLE_EXTENSIONS)

    # ------------------------------------------------------------------
    # 公共 API — 产物
    # ------------------------------------------------------------------

    def get_artifact(self, thread_id: str, path: str) -> tuple[bytes, str]:
        """读取智能体生成的产物文件。

        Args:
            thread_id: 线程 ID。
            path: 虚拟路径（如 "mnt/user-data/outputs/file.txt"）。

        Returns:
            (文件字节, MIME 类型) 元组。

        Raises:
            FileNotFoundError: 产物不存在时抛出。
            ValueError: 路径无效时抛出。
        """
        try:
            actual = get_paths().resolve_virtual_path(thread_id, path)
        except ValueError as exc:
            if "traversal" in str(exc):
                from deerflow.uploads.manager import PathTraversalError

                raise PathTraversalError("Path traversal detected") from exc
            raise
        if not actual.exists():
            raise FileNotFoundError(f"Artifact not found: {path}")
        if not actual.is_file():
            raise ValueError(f"Path is not a file: {path}")

        mime_type, _ = mimetypes.guess_type(actual)
        return actual.read_bytes(), mime_type or "application/octet-stream"
