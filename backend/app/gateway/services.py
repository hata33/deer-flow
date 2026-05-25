"""运行生命周期服务层 — Agent 运行的核心业务逻辑。

本模块集中了创建运行、格式化 SSE 帧和消费 StreamBridge 事件的业务逻辑。
路由模块（thread_runs、runs）是薄 HTTP 处理层，将实际工作委托给本模块。

主要职责：
  1. SSE 帧格式化（format_sse）：按 LangGraph Platform 线格式组装事件帧
  2. 输入/配置归一化（normalize_*）：将 LangGraph Platform 格式转换为内部格式
  3. 运行配置构建（build_run_config）：组装 RunnableConfig，处理自定义 Agent 路由
  4. 运行启动（start_run）：创建 RunRecord 并启动后台 Agent 任务
  5. SSE 消费（sse_consumer）：从 StreamBridge 读取事件并生成 SSE 帧

核心设计：
  - format_sse 输出格式与 LangGraph Platform 线格式一致，供 useStream React Hook
    和 Python langgraph-sdk SSE 解码器消费
  - 自定义 Agent 通过 agent_name 在 configurable/context 中路由，
    make_lead_agent 读取该键加载对应的 SOUL.md 和配置
  - 运行上下文白名单机制（_CONTEXT_CONFIGURABLE_KEYS）控制哪些客户端参数
    可以注入到运行配置中，防止注入无关参数
  - 认证用户上下文注入确保后台工具执行时也能获取正确的用户身份
  - 线程元数据自动 upsert 保证 /threads/search 端点能看到所有线程，
    包括通过无状态运行隐式创建的线程
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import HumanMessage

from app.gateway.deps import get_run_context, get_run_manager, get_stream_bridge
from app.gateway.utils import sanitize_log_param
from deerflow.config.app_config import get_app_config
from deerflow.runtime import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    ConflictError,
    DisconnectMode,
    RunManager,
    RunRecord,
    RunStatus,
    StreamBridge,
    UnsupportedStrategyError,
    run_agent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSE 格式化
# ---------------------------------------------------------------------------


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """格式化单个 SSE（Server-Sent Events）帧。

    字段顺序：event: → data: → id:（可选）→ 空行。
    与 LangGraph Platform 线格式一致，供 useStream React Hook 和
    Python langgraph-sdk SSE 解码器消费。

    Args:
        event: SSE 事件名称（如 "values"、"messages"、"end"）。
        data: 事件数据，将被 JSON 序列化。
        event_id: 可选的事件 ID，用于 Last-Event-ID 恢复。

    Returns:
        格式化的 SSE 帧字符串。
    """
    payload = json.dumps(data, default=str, ensure_ascii=False)
    parts = [f"event: {event}", f"data: {payload}"]
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append("")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 输入/配置辅助函数
# ---------------------------------------------------------------------------


def normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    """将 stream_mode 参数归一化为列表。

    默认值匹配 useStream 的期望：values + messages-tuple。

    Args:
        raw: 原始 stream_mode 值，可能是字符串、列表或 None。

    Returns:
        归一化后的 stream_mode 列表。
    """
    if raw is None:
        return ["values"]
    if isinstance(raw, str):
        return [raw]
    return raw if raw else ["values"]


def normalize_input(raw_input: dict[str, Any] | None) -> dict[str, Any]:
    """将 LangGraph Platform 输入格式转换为 LangChain 状态字典。

    将 OpenAI 风格的消息格式（role/content）转换为 LangChain 的
    HumanMessage 等类型化消息对象。

    Args:
        raw_input: LangGraph Platform 格式的输入字典。

    Returns:
        转换后的状态字典。
    """
    if raw_input is None:
        return {}
    messages = raw_input.get("messages")
    if messages and isinstance(messages, list):
        converted = []
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", msg.get("type", "user"))
                content = msg.get("content", "")
                if role in ("user", "human"):
                    converted.append(HumanMessage(content=content))
                else:
                    # TODO: 处理其他消息类型（system、ai、tool）
                    converted.append(HumanMessage(content=content))
            else:
                converted.append(msg)
        return {**raw_input, "messages": converted}
    return raw_input


# 默认的助手 ID，对应主 Agent
_DEFAULT_ASSISTANT_ID = "lead_agent"


# LangGraph 兼容层从 body.context 转发到运行配置的白名单键。
# LangGraph >=0.6 中 config["context"] 存在，但这些值必须同时写入
# configurable（供旧版 _get_runtime_config 消费者）和 context，
# 因为 LangGraph >=1.1.9 不再将 ToolRuntime.context 回退到 configurable。
_CONTEXT_CONFIGURABLE_KEYS: frozenset[str] = frozenset(
    {
        "model_name",
        "mode",
        "thinking_enabled",
        "reasoning_effort",
        "is_plan_mode",
        "subagent_enabled",
        "max_concurrent_subagents",
        "agent_name",
        "is_bootstrap",
    }
)


def merge_run_context_overrides(config: dict[str, Any], context: Mapping[str, Any] | None) -> None:
    """将白名单中的 body.context 键合并到 config['configurable'] 和 config['context']。

    确保这些键对旧版 configurable 读者和 LangGraph ToolRuntime.context 消费者
    （如 setup_agent 工具）均可见。

    Args:
        config: 运行配置字典，将被就地修改。
        context: 请求体中的 context 字段。
    """
    if not context:
        return
    configurable = config.setdefault("configurable", {})
    runtime_context = config.setdefault("context", {})
    for key in _CONTEXT_CONFIGURABLE_KEYS:
        if key in context:
            if isinstance(configurable, dict):
                configurable.setdefault(key, context[key])
            if isinstance(runtime_context, dict):
                runtime_context.setdefault(key, context[key])


def inject_authenticated_user_context(config: dict[str, Any], request: Request) -> None:
    """将认证用户身份注入运行上下文，供后台工具使用。

    工具执行可能在请求处理函数返回后发生，因此需要持久化用户身份的工具
    不应仅依赖 ContextVar（请求结束后可能失效）。此值来自服务端认证状态，
    不受客户端上下文影响。

    Args:
        config: 运行配置字典，将被就地修改。
        request: FastAPI 请求对象，包含认证用户信息。
    """

    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if user_id is None:
        return

    runtime_context = config.setdefault("context", {})
    if isinstance(runtime_context, dict):
        runtime_context["user_id"] = str(user_id)


def resolve_agent_factory(assistant_id: str | None):
    """根据 assistant_id 解析 Agent 工厂函数。

    自定义 Agent 通过 lead_agent + 注入 configurable/context 中的
    agent_name 实现 — 参见 build_run_config。所有 assistant_id 值
    都映射到同一个工厂函数；路由发生在 make_lead_agent 内部读取
    cfg["agent_name"] 时。

    Args:
        assistant_id: 助手 ID，None 或 "lead_agent" 表示默认 Agent。

    Returns:
        make_lead_agent 工厂函数。
    """
    from deerflow.agents.lead_agent.agent import make_lead_agent

    return make_lead_agent


def build_run_config(
    thread_id: str,
    request_config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    *,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    """为 Agent 构建 RunnableConfig 字典。

    当 assistant_id 引用自定义 Agent（非 "lead_agent"/None）时，
    名称被转发到运行时选项容器中的 agent_name：LangGraph >= 0.6.0 请求
    使用 context，否则使用 configurable。make_lead_agent 读取此键加载
    对应的 agents/<name>/SOUL.md 和每个 Agent 的配置 — 缺少此键时
    Agent 会静默运行为默认 lead_agent。

    此逻辑与频道管理器的 _resolve_run_params 保持一致，确保 LangGraph
    Platform 兼容的 HTTP API 和 IM 频道路径行为一致。

    Args:
        thread_id: 线程 ID。
        request_config: 客户端提供的配置字典。
        metadata: 运行元数据。
        assistant_id: 助手 ID（None 或 "lead_agent" 表示默认）。

    Returns:
        完整的 RunnableConfig 字典。

    Raises:
        ValueError: assistant_id 格式无效或 context 字段类型错误。
    """
    config: dict[str, Any] = {"recursion_limit": 100}
    if request_config:
        # LangGraph >= 0.6.0 引入 context 作为传递线程级数据的首选方式，
        # 并拒绝同时包含 configurable 和 context 的请求。如果调用方已发送
        # context，则尊重它并跳过我们自己的 configurable 字典。
        if "context" in request_config:
            if "configurable" in request_config:
                logger.warning(
                    "build_run_config: client sent both 'context' and 'configurable'; preferring 'context' (LangGraph >= 0.6.0). thread_id=%s, caller_configurable keys=%s",
                    thread_id,
                    list(request_config.get("configurable", {}).keys()),
                )
            context_value = request_config["context"]
            if context_value is None:
                context = {}
            elif isinstance(context_value, Mapping):
                context = dict(context_value)
            else:
                raise ValueError("request config 'context' must be a mapping or null.")
            config["context"] = context
        else:
            configurable = {"thread_id": thread_id}
            configurable.update(request_config.get("configurable", {}))
            config["configurable"] = configurable
        for k, v in request_config.items():
            if k not in ("configurable", "context"):
                config[k] = v
    else:
        config["configurable"] = {"thread_id": thread_id}

    # 注入自定义 Agent 名称：当调用方指定了非默认 assistant 时。
    # 尊重活跃运行时选项容器中已有的显式 agent_name。
    if assistant_id and assistant_id != _DEFAULT_ASSISTANT_ID:
        normalized = assistant_id.strip().lower().replace("_", "-")
        if not normalized or not re.fullmatch(r"[a-z0-9-]+", normalized):
            raise ValueError(f"Invalid assistant_id {assistant_id!r}: must contain only letters, digits, and hyphens after normalization.")
        if "configurable" in config:
            target = config["configurable"]
        elif "context" in config:
            target = config["context"]
        else:
            target = config.setdefault("configurable", {})
        if target is not None and "agent_name" not in target:
            target["agent_name"] = normalized
    if metadata:
        config.setdefault("metadata", {}).update(metadata)
    return config


# ---------------------------------------------------------------------------
# 运行生命周期
# ---------------------------------------------------------------------------


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
) -> RunRecord:
    """创建 RunRecord 并启动后台 Agent 任务。

    执行流程：
      1. 验证模型名称（如果在 context 中指定）
      2. 通过 RunManager 创建运行记录（处理并发冲突和策略）
      3. Upsert 线程元数据确保线程在搜索中可见
      4. 构建运行配置并注入用户上下文
      5. 启动 asyncio 后台任务执行 Agent

    Args:
        body: 已验证的请求体（RunCreateRequest，类型标注为 Any 避免循环导入）。
        thread_id: 目标线程 ID。
        request: FastAPI 请求对象 — 用于从 app.state 获取单例。

    Returns:
        创建的 RunRecord 实例。

    Raises:
        HTTPException 400: 模型不在白名单中。
        HTTPException 409: 运行冲突（已有运行进行中）。
        HTTPException 501: 不支持的多任务策略。
    """
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    run_ctx = get_run_context(request)

    disconnect = DisconnectMode.cancel if body.on_disconnect == "cancel" else DisconnectMode.continue_

    body_context = getattr(body, "context", None) or {}
    model_name = body_context.get("model_name")

    # 将非字符串的 model_name 强制转换为字符串后再截断
    if model_name is not None and not isinstance(model_name, str):
        model_name = str(model_name)

    # 当提供了 model_name 时，验证其是否在模型白名单中
    if model_name:
        app_config = get_app_config()
        resolved = app_config.get_model_config(model_name)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Model {model_name!r} is not in the configured model allowlist",
            )

    try:
        record = await run_mgr.create_or_reject(
            thread_id,
            body.assistant_id,
            on_disconnect=disconnect,
            metadata=body.metadata or {},
            kwargs={"input": body.input, "config": body.config},
            multitask_strategy=body.multitask_strategy,
            model_name=model_name,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsupportedStrategyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    # Upsert 线程元数据，使线程出现在 /threads/search 中，
    # 包括从未通过 POST /threads 显式创建的线程（如无状态运行）。
    try:
        existing = await run_ctx.thread_store.get(thread_id)
        if existing is None:
            await run_ctx.thread_store.create(
                thread_id,
                assistant_id=body.assistant_id,
                metadata=body.metadata,
            )
        else:
            await run_ctx.thread_store.update_status(thread_id, "running")
    except Exception:
        logger.warning("Failed to upsert thread_meta for %s (non-fatal)", sanitize_log_param(thread_id))

    agent_factory = resolve_agent_factory(body.assistant_id)
    graph_input = normalize_input(body.input)
    config = build_run_config(thread_id, body.config, body.metadata, assistant_id=body.assistant_id)

    # 将 DeerFlow 特定的上下文覆盖合并到 configurable 和 context 中。
    # context 字段是 langgraph 兼容层的自定义扩展，承载 Agent 配置
    # （model_name、thinking_enabled 等）。只转发 Agent 相关的键，
    # 忽略未知键（如 thread_id）。
    merge_run_context_overrides(config, getattr(body, "context", None))
    inject_authenticated_user_context(config, request)

    stream_modes = normalize_stream_modes(body.stream_mode)

    task = asyncio.create_task(
        run_agent(
            bridge,
            run_mgr,
            record,
            ctx=run_ctx,
            agent_factory=agent_factory,
            graph_input=graph_input,
            config=config,
            stream_modes=stream_modes,
            stream_subgraphs=body.stream_subgraphs,
            interrupt_before=body.interrupt_before,
            interrupt_after=body.interrupt_after,
        )
    )
    record.task = task

    # 标题同步由 worker.py 的 finally 块处理，它从检查点读取标题
    # 并在运行完成后调用 thread_store.update_display_name。

    return record


async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
):
    """异步生成器：从 StreamBridge 读取事件并生成 SSE 帧。

    finally 块实现 on_disconnect 语义：
      - cancel：客户端断开连接时中止后台任务
      - continue：让任务继续运行，丢弃事件

    Args:
        bridge: SSE 事件桥接器。
        record: 运行记录。
        request: FastAPI 请求对象（用于检测断开连接）。
        run_mgr: 运行管理器（用于取消运行）。
    """
    last_event_id = request.headers.get("Last-Event-ID")
    try:
        async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):
            if await request.is_disconnected():
                break

            if entry is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None, event_id=entry.id or None)
                return

            yield format_sse(entry.event, entry.data, event_id=entry.id or None)

    finally:
        # 客户端断开后，根据 on_disconnect 策略决定是否取消运行
        if record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
