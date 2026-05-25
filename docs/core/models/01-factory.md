# 模型工厂详解

模型工厂是 `models/` 模块的核心入口，由 `factory.py` 中的 `create_chat_model()` 函数实现。它负责将 `config.yaml` 中的声明式配置转化为可调用的 `BaseChatModel` 实例。

## 函数签名

```python
def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    **kwargs,
) -> BaseChatModel
```

| 参数 | 说明 |
|------|------|
| `name` | 模型名称，对应 `config.yaml` 中的 `models[].name`。为 `None` 时使用配置中的第一个模型 |
| `thinking_enabled` | 是否启用扩展思考模式，影响 `when_thinking_enabled` / `when_thinking_disabled` 参数注入 |
| `app_config` | 可选的 `AppConfig` 实例，为 `None` 时调用 `get_app_config()` 获取全局配置 |
| `**kwargs` | 额外参数，直接传递给 Provider 构造函数（如 `reasoning_effort`） |

## 完整创建流程

```
create_chat_model(name, thinking_enabled)
  │
  ├─ 1. 获取配置 → get_app_config()
  ├─ 2. 查找模型配置 → config.get_model_config(name)
  ├─ 3. 反射创建类 → resolve_class(model_config.use, BaseChatModel)
  ├─ 4. 序列化配置 → model_config.model_dump(exclude_none=True, exclude={...})
  ├─ 5. 合并 thinking 参数 → when_thinking_enabled / when_thinking_disabled
  ├─ 6. Provider 特殊处理 → Codex、MindIE、stream_usage
  ├─ 7. 实例化 → model_class(**kwargs, **model_settings)
  └─ 8. 挂载追踪回调 → build_tracing_callbacks()
```

### 步骤 1：获取配置

```python
config = app_config or get_app_config()
```

如果调用者提供了 `app_config` 参数（例如在测试或特定运行时上下文中），则直接使用。否则通过 `get_app_config()` 获取全局缓存的配置实例，该实例会自动检测 `config.yaml` 的 `mtime` 变更并重新加载。

### 步骤 2：查找模型配置

```python
if name is None:
    name = config.models[0].name
model_config = config.get_model_config(name)
if model_config is None:
    raise ValueError(f"Model {name} not found in config")
```

`name` 为 `None` 时，默认使用 `config.yaml` 中 `models` 列表的第一个模型。`get_model_config()` 按名称遍历查找。

### 步骤 3：反射创建类

```python
model_class = resolve_class(model_config.use, BaseChatModel)
```

`resolve_class()` 解析 `use` 字段（如 `deerflow.models.claude_provider:ClaudeChatModel`），动态导入模块并获取类对象，同时验证它是否为 `BaseChatModel` 的子类。

**错误处理**：如果模块未安装（例如缺少 `langchain-google-genai`），反射系统会生成可操作的安装提示，帮助用户快速定位问题。

### 步骤 4：序列化配置

```python
model_settings_from_config = model_config.model_dump(
    exclude_none=True,
    exclude={
        "use", "name", "display_name", "description",
        "supports_thinking", "supports_reasoning_effort",
        "when_thinking_enabled", "when_thinking_disabled",
        "thinking", "supports_vision",
    },
)
```

将 `ModelConfig` 序列化为字典，排除所有框架管理字段。由于 `ModelConfig` 设置了 `extra="allow"`，所有 Provider 特定字段（如 `enable_prompt_caching`、`base_url`、`api_key`）都会被保留。

### 步骤 5：Thinking 参数合并

这是工厂中最复杂的逻辑，根据 `thinking_enabled` 标志动态调整模型参数。

#### 5.1 计算 effective_wte（Effective When-Thinking-Enabled）

```python
has_thinking_settings = (
    model_config.when_thinking_enabled is not None
) or (model_config.thinking is not None)

effective_wte = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}

# thinking 字段是 when_thinking_enabled.thinking 的简写
if model_config.thinking is not None:
    merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
    effective_wte = {**effective_wte, "thinking": merged_thinking}
```

`thinking` 字段会被合并到 `when_thinking_enabled.thinking` 中，提供更简洁的配置写法：

```yaml
# 简写形式
thinking:
  type: enabled
  budget_tokens: 10000

# 等价于
when_thinking_enabled:
  thinking:
    type: enabled
    budget_tokens: 10000
```

#### 5.2 thinking_enabled = True

```python
if thinking_enabled and has_thinking_settings:
    if not model_config.supports_thinking:
        raise ValueError(
            f"Model {name} does not support thinking. "
            f"Set `supports_thinking` to true in the `config.yaml` to enable thinking."
        )
    if effective_wte:
        model_settings_from_config.update(effective_wte)
```

启用 thinking 时，验证 `supports_thinking` 标志，然后将 `effective_wte` 中的参数合并到配置中。

#### 5.3 thinking_enabled = False（多种禁用策略）

禁用 thinking 时，工厂会按照优先级尝试多种策略：

```python
if not thinking_enabled:
    if model_config.when_thinking_disabled is not None:
        # 用户显式配置了禁用参数 → 最高优先级
        model_settings_from_config.update(model_config.when_thinking_disabled)

    elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
        # OpenAI 兼容网关：thinking 嵌套在 extra_body 中
        model_settings_from_config["extra_body"] = _deep_merge_dicts(...)
        model_settings_from_config["reasoning_effort"] = "minimal"

    elif has_thinking_settings and (disable_chat_template_kwargs := ...):
        # vLLM：通过 chat_template_kwargs 切换 thinking
        model_settings_from_config["extra_body"] = _deep_merge_dicts(...)

    elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
        # 原生 langchain_anthropic：thinking 是构造函数参数
        model_settings_from_config["thinking"] = {"type": "disabled"}
```

| 策略 | 适用场景 | 禁用方式 |
|------|----------|----------|
| `when_thinking_disabled` | 用户显式配置 | 直接合并用户参数 |
| `extra_body.thinking.type` | OpenAI 兼容网关 | 设置 `thinking.type = "disabled"` + `reasoning_effort = "minimal"` |
| `extra_body.chat_template_kwargs` | vLLM Qwen 模型 | 设置 `thinking = False` 或 `enable_thinking = False` |
| `thinking.type` | 原生 Anthropic | 设置 `thinking.type = "disabled"` |

### 步骤 6：Provider 特殊处理

#### 6.1 reasoning_effort 清理

```python
if not model_config.supports_reasoning_effort:
    kwargs.pop("reasoning_effort", None)
    model_settings_from_config.pop("reasoning_effort", None)
```

不支持 `reasoning_effort` 的模型会移除该参数，避免传递无效参数。

#### 6.2 stream_usage 自动启用

```python
_enable_stream_usage_by_default(model_config.use, model_settings_from_config)
```

对于 OpenAI 兼容的模型（使用自定义 `base_url`），LangChain 默认不会启用 `stream_usage`，导致 TokenUsageMiddleware 无法获取 token 统计。工厂会自动启用：

```python
if model_use_path != "langchain_openai:ChatOpenAI":
    return
if "stream_usage" in model_settings_from_config:
    return
if "base_url" in model_settings_from_config or "openai_api_base" in model_settings_from_config:
    model_settings_from_config["stream_usage"] = True
```

#### 6.3 Codex 特殊处理

```python
if issubclass(model_class, CodexChatModel):
    model_settings_from_config.pop("max_tokens", None)  # Codex 不接受 max_tokens
    explicit_effort = kwargs.pop("reasoning_effort", None)
    if not thinking_enabled:
        model_settings_from_config["reasoning_effort"] = "none"
    elif explicit_effort and explicit_effort in ("low", "medium", "high", "xhigh"):
        model_settings_from_config["reasoning_effort"] = explicit_effort
    elif "reasoning_effort" not in model_settings_from_config:
        model_settings_from_config["reasoning_effort"] = "medium"
```

Codex Provider 不支持 `max_tokens` 参数，工厂会自动移除。`reasoning_effort` 映射到 Codex 的 `reasoning.effort`。

#### 6.4 MindIE 特殊处理

```python
if getattr(model_class, "__name__", "") == "MindIEChatModel":
    model_settings_from_config["max_retries"] = model_settings_from_config.get("max_retries", 1)
```

MindIE（华为昇腾 NPU）使用保守的重试策略，默认最多重试 1 次。

#### 6.5 通用 stream_usage 启用

```python
if "stream_usage" not in model_settings_from_config and "stream_usage" not in kwargs:
    if "stream_usage" in getattr(model_class, "model_fields", {}):
        model_settings_from_config["stream_usage"] = True
```

对所有支持 `stream_usage` 字段的模型类，默认启用流式 token 统计。

### 步骤 7：实例化

```python
model_instance = model_class(**kwargs, **model_settings_from_config)
```

将所有参数传递给 Provider 构造函数。`kwargs`（来自调用者）的优先级高于 `model_settings_from_config`（来自配置文件），因为 Python 字典解包的后者会覆盖前者。

### 步骤 8：挂载追踪回调

```python
callbacks = build_tracing_callbacks()
if callbacks:
    existing_callbacks = model_instance.callbacks or []
    model_instance.callbacks = [*existing_callbacks, *callbacks]
```

从全局追踪配置中构建回调（如 LangSmith 追踪），追加到模型实例的回调列表中。

## 辅助函数

### _deep_merge_dicts

```python
def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    """递归合并两个字典，不修改原始输入。"""
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged
```

用于 thinking 禁用时合并 `extra_body` 配置。`override` 中的值优先于 `base`。

### _vllm_disable_chat_template_kwargs

```python
def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    """为 vLLM 构建禁用 thinking 的参数。"""
    disable_kwargs = {}
    if "thinking" in chat_template_kwargs:
        disable_kwargs["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable_kwargs["enable_thinking"] = False
    return disable_kwargs
```

同时处理 `thinking`（旧名）和 `enable_thinking`（vLLM 0.19.0 新名）两个参数。

## 配置合并策略总览

工厂的配置合并遵循以下优先级（从低到高）：

```
ModelConfig 基础字段
  ↓ 合并
when_thinking_enabled / when_thinking_disabled（根据 thinking 状态）
  ↓ 合并
Provider 特殊处理（Codex reasoning_effort、MindIE max_retries 等）
  ↓ 合并
**kwargs（来自调用者的显式参数）
```

## 错误处理

| 错误场景 | 处理方式 |
|----------|----------|
| 模型名不存在 | `ValueError: Model {name} not found in config` |
| thinking 不支持 | `ValueError: Model {name} does not support thinking` |
| Provider 模块未安装 | 反射系统提供安装提示（如 `uv add langchain-google-genai`） |
| 环境变量不存在 | `ValueError: Environment variable {VAR} not found` |
