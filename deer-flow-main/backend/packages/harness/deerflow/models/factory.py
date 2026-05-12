"""模型工厂——从配置创建 LangChain 聊天模型实例。

核心函数 create_chat_model 根据 config.yaml 中的模型定义，
通过 resolve_class 反射加载模型类，合并配置参数，
处理 thinking 模式和 reasoning_effort 的模型能力差异，
并可选注入 LangSmith 追踪器。

支持的模型能力适配：
- thinking 模式：根据 supports_thinking 决定是否启用
- reasoning_effort：不支持时自动移除
- Codex Responses API：特殊的 reasoning_effort 映射
"""

import logging

from langchain.chat_models import BaseChatModel

from deerflow.config import get_app_config, get_tracing_config, is_tracing_enabled
from deerflow.reflection import resolve_class

logger = logging.getLogger(__name__)


def create_chat_model(name: str | None = None, thinking_enabled: bool = False, **kwargs) -> BaseChatModel:
    """从配置创建聊天模型实例。

    流程：
    1. 解析模型名称（未指定时使用配置中的第一个模型）
    2. 通过 resolve_class 反射加载模型类
    3. 合并配置参数，处理 thinking 模式和 reasoning_effort
    4. 对 Codex 模型做特殊参数映射
    5. 可选注入 LangSmith 追踪器

    Args:
        name: 模型名称，为 None 时使用配置中的第一个模型。
        thinking_enabled: 是否启用思考模式。
        **kwargs: 额外的模型构造参数（如 reasoning_effort）。

    Returns:
        配置完成的 BaseChatModel 实例。

    Raises:
        ValueError: 模型未找到或不支持所请求的功能。
    """
    config = get_app_config()
    if name is None:
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None

    # 通过反射加载模型类
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 序列化模型配置为构造参数（排除非构造字段）
    model_settings_from_config = model_config.model_dump(
        exclude_none=True,
        exclude={
            "use",
            "name",
            "display_name",
            "description",
            "supports_thinking",
            "supports_reasoning_effort",
            "when_thinking_enabled",
            "thinking",
            "supports_vision",
        },
    )

    # 计算 thinking 模式的合并配置
    # thinking 字段是 when_thinking_enabled["thinking"] 的快捷方式
    has_thinking_settings = (model_config.when_thinking_enabled is not None) or (model_config.thinking is not None)
    effective_wte: dict = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    if model_config.thinking is not None:
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}

    # 启用 thinking 时合并 when_thinking_enabled 参数
    if thinking_enabled and has_thinking_settings:
        if not model_config.supports_thinking:
            raise ValueError(f"Model {name} does not support thinking. Set `supports_thinking` to true in the `config.yaml` to enable thinking.") from None
        if effective_wte:
            model_settings_from_config.update(effective_wte)

    # 禁用 thinking 时显式发送 disabled 指令（防止模型默认启用）
    if not thinking_enabled and has_thinking_settings:
        if effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            # OpenAI 兼容网关：thinking 嵌套在 extra_body 中
            kwargs.update({"extra_body": {"thinking": {"type": "disabled"}}})
            kwargs.update({"reasoning_effort": "minimal"})
        elif effective_wte.get("thinking", {}).get("type"):
            # 原生 langchain_anthropic：thinking 是直接构造参数
            kwargs.update({"thinking": {"type": "disabled"}})

    # 不支持 reasoning_effort 的模型自动移除该参数
    if not model_config.supports_reasoning_effort and "reasoning_effort" in kwargs:
        del kwargs["reasoning_effort"]

    # Codex Responses API 模型的特殊参数映射
    from deerflow.models.openai_codex_provider import CodexChatModel

    if issubclass(model_class, CodexChatModel):
        # Codex 端点不接受 max_tokens/max_output_tokens
        model_settings_from_config.pop("max_tokens", None)

        # 将 thinking 模式映射为 reasoning_effort
        explicit_effort = kwargs.pop("reasoning_effort", None)
        if not thinking_enabled:
            model_settings_from_config["reasoning_effort"] = "none"
        elif explicit_effort and explicit_effort in ("low", "medium", "high", "xhigh"):
            model_settings_from_config["reasoning_effort"] = explicit_effort
        elif "reasoning_effort" not in model_settings_from_config:
            model_settings_from_config["reasoning_effort"] = "medium"

    model_instance = model_class(**kwargs, **model_settings_from_config)

    # 可选注入 LangSmith 追踪器
    if is_tracing_enabled():
        try:
            from langchain_core.tracers.langchain import LangChainTracer

            tracing_config = get_tracing_config()
            tracer = LangChainTracer(
                project_name=tracing_config.project,
            )
            existing_callbacks = model_instance.callbacks or []
            model_instance.callbacks = [*existing_callbacks, tracer]
            logger.debug(f"LangSmith tracing attached to model '{name}' (project='{tracing_config.project}')")
        except Exception as e:
            logger.warning(f"Failed to attach LangSmith tracing to model '{name}': {e}")

    return model_instance
