"""模型工厂 — 创建和配置聊天模型实例的核心入口。

模块功能
========
提供 `create_chat_model()` 工厂函数，根据 YAML 配置文件动态创建 LangChain
`BaseChatModel` 实例。这是 DeerFlow 系统中所有模型创建的唯一入口点。

核心设计
========
1. **反射加载**: 通过 `resolve_class()` 将配置中的类路径字符串（如
   `langchain_openai:ChatOpenAI`）动态解析为实际的 Python 类
2. **思维模式适配**: 统一处理不同模型的思维/推理（thinking/reasoning）能力差异，
   包括启用/禁用切换、推理努力级别设定、额外参数注入等
3. **配置合并**: 支持多层配置合并（when_thinking_enabled、when_thinking_disabled、
   thinking 快捷字段），确保不同思维状态下的参数正确性
4. **流式用量追踪**: 为 OpenAI 兼容网关自动启用 stream_usage，
   确保 TokenUsageMiddleware 能获取到用量数据
5. **追踪集成**: 自动将 LangSmith/Langfuse 等追踪回调附加到模型实例

思维模式处理逻辑
================
模型的思维模式（thinking）是本模块最复杂的部分，需要处理多种提供商的差异：

1. **Anthropic 原生**: thinking 是构造函数参数，`type: enabled/disabled`
2. **OpenAI 兼容网关**: thinking 嵌套在 `extra_body.thinking` 中
3. **vLLM/Qwen**: 使用 `chat_template_kwargs` 控制思维开关
4. **Codex Responses API**: 使用 `reasoning_effort: none/low/medium/high` 控制

配置字段说明
============
- `use`: 模型类路径（如 `langchain_openai:ChatOpenAI`）
- `supports_thinking`: 模型是否支持思维模式
- `supports_reasoning_effort`: 是否支持推理努力级别控制
- `when_thinking_enabled`: 思维模式启用时注入的额外参数
- `when_thinking_disabled`: 思维模式禁用时注入的额外参数
- `thinking`: 快捷字段，等效于 `when_thinking_enabled.thinking`
- `supports_vision`: 是否支持图像输入

使用场景
========
在 Agent 节点中创建模型::

    from deerflow.models import create_chat_model

    # 使用配置文件中的第一个模型
    model = create_chat_model()

    # 使用指定名称的模型，启用思维模式
    model = create_chat_model("claude-sonnet", thinking_enabled=True)

注意事项
========
- 如果模型不支持思维模式但请求启用，会抛出 ValueError
- Codex 模型不支持 max_tokens/max_output_tokens，会被自动移除
- MindIE 模型的 max_retries 默认设为 1 以防止级联超时
"""

import logging

from langchain.chat_models import BaseChatModel

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_class
from deerflow.tracing import build_tracing_callbacks

logger = logging.getLogger(__name__)


def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    """递归合并两个字典，不修改输入字典。

    当两个字典的相同键都是字典类型时，递归合并内层字典；
    否则 override 中的值覆盖 base 中的值。

    Args:
        base: 基础字典（可被 None 替代）。
        override: 覆盖字典，其值优先级更高。

    Returns:
        dict: 合并后的新字典。
    """
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            # 同层同键均为字典时递归合并
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    """为 vLLM/Qwen 构建禁用思维的 chat_template_kwargs。

    vLLM 使用 `chat_template_kwargs` 中的 `thinking` 或 `enable_thinking` 字段
    控制思维模式开关。此函数将对应的字段设为 False 以禁用思维。

    Args:
        chat_template_kwargs: 当前的 chat_template_kwargs 配置。

    Returns:
        dict: 包含禁用标志的 kwargs 字典。
    """
    disable_kwargs: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable_kwargs["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable_kwargs["enable_thinking"] = False
    return disable_kwargs


def _enable_stream_usage_by_default(model_use_path: str, model_settings_from_config: dict) -> None:
    """为 OpenAI 兼容模型默认启用流式用量追踪。

    LangChain 仅在使用原生 OpenAI 端点时自动启用 `stream_usage`。
    DeerFlow 频繁使用 OpenAI 兼容的第三方网关（如豆包、DeepSeek），
    若不手动启用，TokenUsageMiddleware 将无法获取用量数据。

    仅在以下条件同时满足时生效：
    - 模型类为 langchain_openai:ChatOpenAI
    - 用户未显式配置 stream_usage
    - 配置中包含 base_url 或 openai_api_base（说明使用了自定义端点）

    Args:
        model_use_path: 模型类的完整路径字符串。
        model_settings_from_config: 从配置文件解析的模型设置字典。会被就地修改。
    """
    if model_use_path != "langchain_openai:ChatOpenAI":
        return
    # 用户已显式配置，不覆盖
    if "stream_usage" in model_settings_from_config:
        return
    # 仅在使用自定义端点时自动启用
    if "base_url" in model_settings_from_config or "openai_api_base" in model_settings_from_config:
        model_settings_from_config["stream_usage"] = True


def create_chat_model(name: str | None = None, thinking_enabled: bool = False, *, app_config: AppConfig | None = None, **kwargs) -> BaseChatModel:
    """根据配置创建聊天模型实例。

    这是 DeerFlow 中创建 LLM 实例的核心工厂函数。它根据 config.yaml 中的模型配置，
    动态加载对应的模型类并实例化。支持思维模式切换、视觉能力、推理努力级别等
    高级特性的自动配置。

    执行流程：
    1. 加载应用配置，查找指定名称的模型配置
    2. 通过反射机制解析模型类路径为实际的 Python 类
    3. 序列化模型配置，排除元数据字段
    4. 处理思维模式的启用/禁用参数注入
    5. 针对特定模型类进行特殊处理（Codex、MindIE 等）
    6. 创建模型实例并附加追踪回调

    Args:
        name: 模型名称（对应 config.yaml 中的 name 字段）。
            如果为 None，使用配置中的第一个模型。
        thinking_enabled: 是否启用思维/推理模式。
            启用后会注入 when_thinking_enabled 中的参数。
        app_config: 可选的应用配置对象。如果为 None，使用全局配置。
        **kwargs: 额外的模型构造参数，会覆盖配置文件中的设置。

    Returns:
        BaseChatModel: 配置完成的 LangChain 聊天模型实例。

    Raises:
        ValueError: 当模型名称不存在于配置中时。
        ValueError: 当请求对不支持思维的模型启用思维模式时。
    """
    # 加载应用配置
    config = app_config or get_app_config()
    if name is None:
        # 未指定模型名称时，使用配置中的第一个模型
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None

    # 通过反射将类路径字符串解析为实际的 Python 类
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 序列化模型配置，排除仅用于元数据的字段
    model_settings_from_config = model_config.model_dump(
        exclude_none=True,
        exclude={
            "use",                    # 类路径，已通过 resolve_class 使用
            "name",                   # 模型名称，已用于查找
            "display_name",           # 显示名称，仅供 UI 使用
            "description",            # 描述信息，仅供 UI 使用
            "supports_thinking",      # 能力标记，已用于逻辑判断
            "supports_reasoning_effort",  # 能力标记
            "when_thinking_enabled",  # 思维模式参数，后续单独处理
            "when_thinking_disabled", # 思维模式参数，后续单独处理
            "thinking",               # 思维快捷字段，后续单独处理
            "supports_vision",        # 能力标记
        },
    )

    # ---- 思维模式参数处理 ----
    # 计算有效的 when_thinking_enabled 配置，合并 thinking 快捷字段
    has_thinking_settings = (model_config.when_thinking_enabled is not None) or (model_config.thinking is not None)
    effective_wte: dict = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    if model_config.thinking is not None:
        # thinking 快捷字段等效于 when_thinking_enabled["thinking"]，进行合并
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}

    if thinking_enabled and has_thinking_settings:
        # 启用思维模式：检查模型是否支持，然后注入启用参数
        if not model_config.supports_thinking:
            raise ValueError(f"Model {name} does not support thinking. Set `supports_thinking` to true in the `config.yaml` to enable thinking.") from None
        if effective_wte:
            model_settings_from_config.update(effective_wte)

    if not thinking_enabled:
        # 禁用思维模式：根据不同提供商使用不同的禁用策略
        if model_config.when_thinking_disabled is not None:
            # 用户提供了自定义的禁用配置，优先使用
            model_settings_from_config.update(model_config.when_thinking_disabled)
        elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            # OpenAI 兼容网关：thinking 嵌套在 extra_body 中
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"thinking": {"type": "disabled"}},
            )
            model_settings_from_config["reasoning_effort"] = "minimal"
        elif has_thinking_settings and (disable_chat_template_kwargs := _vllm_disable_chat_template_kwargs(effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {})):
            # vLLM：通过 chat_template_kwargs 控制思维开关
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"chat_template_kwargs": disable_chat_template_kwargs},
            )
        elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
            # Anthropic 原生：thinking 是直接的构造函数参数
            model_settings_from_config["thinking"] = {"type": "disabled"}

    # 如果模型不支持推理努力级别，移除相关参数
    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings_from_config.pop("reasoning_effort", None)

    # 为使用自定义端点的 OpenAI 兼容模型自动启用 stream_usage
    _enable_stream_usage_by_default(model_config.use, model_settings_from_config)

    # ---- Codex Responses API 模型的特殊处理 ----
    from deerflow.models.openai_codex_provider import CodexChatModel

    if issubclass(model_class, CodexChatModel):
        # Codex 端点不支持 max_tokens/max_output_tokens 参数，必须移除
        model_settings_from_config.pop("max_tokens", None)

        # 将思维模式映射为 reasoning_effort 参数
        explicit_effort = kwargs.pop("reasoning_effort", None)
        if not thinking_enabled:
            # 禁用思维模式时使用 "none"
            model_settings_from_config["reasoning_effort"] = "none"
        elif explicit_effort and explicit_effort in ("low", "medium", "high", "xhigh"):
            # 使用前端显式传入的推理努力级别
            model_settings_from_config["reasoning_effort"] = explicit_effort
        elif "reasoning_effort" not in model_settings_from_config:
            # 默认使用 medium 级别
            model_settings_from_config["reasoning_effort"] = "medium"

    # ---- MindIE 模型的特殊处理 ----
    # 强制使用保守的重试默认值，超时规范化由 MindIEChatModel 内部处理
    if getattr(model_class, "__name__", "") == "MindIEChatModel":
        # 限制最大重试次数，防止级联超时导致系统不可用
        model_settings_from_config["max_retries"] = model_settings_from_config.get("max_retries", 1)

    # ---- 流式用量追踪默认启用 ----
    # LangChain 的 BaseChatOpenAI 仅在不使用自定义 base_url/api_base 时
    # 才默认启用 stream_usage=True。对于使用第三方端点的模型（如豆包、DeepSeek），
    # 用量数据会被静默丢弃。此处默认启用，除非用户显式配置。
    if "stream_usage" not in model_settings_from_config and "stream_usage" not in kwargs:
        if "stream_usage" in getattr(model_class, "model_fields", {}):
            model_settings_from_config["stream_usage"] = True

    # 创建模型实例：合并 kwargs（运行时参数）和配置文件参数
    model_instance = model_class(**kwargs, **model_settings_from_config)

    # 附加 LangSmith/Langfuse 等追踪回调
    callbacks = build_tracing_callbacks()
    if callbacks:
        existing_callbacks = model_instance.callbacks or []
        model_instance.callbacks = [*existing_callbacks, *callbacks]
        logger.debug(f"Tracing attached to model '{name}' with providers={len(callbacks)}")
    return model_instance
