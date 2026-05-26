# 05 - 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/models/` 目录下的源码，逐层拆解模型系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌───────────────────────────────────────────────────────────────────┐
│                        调用方（外部世界）                           │
│                                                                    │
│  agents/lead_agent/agent.py      agents/lead_agent/prompt.py      │
│  ┌───────────────────────────┐   ┌──────────────────────────┐    │
│  │ make_lead_agent()         │   │ create_chat_model()      │    │
│  │  └─ create_chat_model()  │   │  (用于标题、记忆等子模型) │    │
│  └─────────────┬─────────────┘   └────────────┬─────────────┘    │
│                │                               │                   │
│                │ ①请求创建模型                  │                   │
└────────────────┼───────────────────────────────┼───────────────────┘
                 │                               │
┌────────────────▼───────────────────────────────▼───────────────────┐
│                      models 包（内部世界）                           │
│                                                                     │
│  __init__.py ─── 统一导出 create_chat_model                         │
│                                                                     │
│  ┌──────────────────┐                                               │
│  │ factory.py       │── 核心工厂                                    │
│  │                  │                                               │
│  │ ② resolve_class │──→ reflection/resolve_class()                 │
│  │ ③ 配置合并       │                                               │
│  │ ④ thinking 切换  │                                               │
│  │ ⑤ 实例化 + 追踪  │                                               │
│  └────────┬─────────┘                                               │
│           │                                                         │
│  ┌────────▼──────────────────────────────────────────────────┐     │
│  │                  Provider 子类层                            │     │
│  │                                                            │     │
│  │  ┌────────────────┐ ┌─────────────┐ ┌──────────────────┐ │     │
│  │  │claude_provider │ │vllm_provider│ │openai_codex_     │ │     │
│  │  │                │ │             │ │  provider        │ │     │
│  │  │ OAuth Bearer   │ │ reasoning   │ │ Responses API    │ │     │
│  │  │ Prompt Cache   │ │ 字段保留    │ │ SSE 流式收集     │ │     │
│  │  │ Thinking Budget│ │ 消息匹配    │ │ reasoning_effort │ │     │
│  │  └───────┬────────┘ └──────┬──────┘ └────────┬─────────┘ │     │
│  │          │                  │                  │           │     │
│  │  ┌───────▼──────────────────▼──────────────────▼─────────┐ │     │
│  │  │  patched_*.py 补丁层                                    │ │     │
│  │  │  deepseek: reasoning_content 保留                      │ │     │
│  │  │  minimax:   reasoning_details 解析 + <think 标签剥离   │ │     │
│  │  │  openai:    thought_signature 保留（Gemini 兼容）      │ │     │
│  │  └───────────────────────────────────────────────────────┘ │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                     │
│  ┌──────────────────────┐                                           │
│  │ credential_loader.py │── Claude Code OAuth + Codex CLI 凭证加载  │
│  └──────────────────────┘                                           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：工厂流程 — 从配置到实例

### 2.1 完整的创建流水线

```
create_chat_model(name, thinking_enabled)
  │
  ├─ get_app_config()                     ← 加载配置（带缓存）
  │   └─ config.get_model_config(name)    ← 按名称查找 ModelConfig
  │
  ├─ resolve_class(model_config.use)      ← 反射加载 Provider 类
  │   └─ importlib → issubclass(BaseChatModel)  ← 类型校验
  │
  ├─ model_config.model_dump(exclude=...) ← 序列化，排除元数据字段
  │
  ├─ thinking 参数处理                     ← 4 种 Provider 分支
  │
  ├─ _enable_stream_usage_by_default()    ← OpenAI 兼容网关自动启用
  │
  ├─ Provider 特殊处理
  │   ├─ Codex: 移除 max_tokens, 映射 reasoning_effort
  │   └─ MindIE: 限制 max_retries=1
  │
  ├─ model_class(**kwargs, **settings)    ← 实例化
  │
  └─ build_tracing_callbacks()            ← 附加 LangSmith/Langfuse
```

### 2.2 配置序列化的排除策略

```python
model_settings_from_config = model_config.model_dump(
    exclude_none=True,
    exclude={
        "use",                    # 类路径，已通过 resolve_class 使用
        "name",                   # 模型名称，已用于查找
        "display_name",           # 仅 UI
        "description",            # 仅 UI
        "supports_thinking",      # 能力标记，已用于逻辑判断
        "supports_reasoning_effort",
        "when_thinking_enabled",  # thinking 参数，后续单独处理
        "when_thinking_disabled",
        "thinking",               # thinking 快捷字段
        "supports_vision",        # 能力标记
    },
)
```

这些字段是"元数据"——影响工厂行为但不传递给 Provider 构造函数。`extra="allow"` 确保所有未排除的字段（如 `api_key`、`base_url`、`max_tokens`）直接透传。

---

## 三、第 2 层：Thinking 配置合并

### 3.1 thinking 快捷字段与 when_thinking_enabled 的合并

```
config.yaml:
  thinking:                              # 快捷字段
    type: enabled
  when_thinking_enabled:                 # 完整配置
    extra_body:
      chat_template_kwargs:
        enable_thinking: true

合并后 effective_wte:
  {
    "extra_body": {
      "chat_template_kwargs": {
        "enable_thinking": true
      }
    },
    "thinking": {
      "type": "enabled"                  # 从快捷字段合并
    }
  }
```

实现：递归合并 `_deep_merge_dicts()`，快捷字段的 `thinking` 键与 `when_thinking_enabled.thinking` 内层合并，而非覆盖。

### 3.2 禁用 thinking 的 4 条分支

```python
if not thinking_enabled:
    if model_config.when_thinking_disabled is not None:
        # 分支 1：用户自定义禁用配置
        model_settings.update(model_config.when_thinking_disabled)

    elif ... extra_body.thinking.type ... :
        # 分支 2：OpenAI 兼容网关
        #   → extra_body.thinking.type = "disabled"
        #   → reasoning_effort = "minimal"

    elif ... chat_template_kwargs ... :
        # 分支 3：vLLM/Qwen
        #   → extra_body.chat_template_kwargs.thinking = False
        #   → extra_body.chat_template_kwargs.enable_thinking = False

    elif ... thinking.type ... :
        # 分支 4：Anthropic 原生
        #   → thinking = {"type": "disabled"}
```

优先级：用户自定义 > OpenAI 网关 > vLLM > Anthropic。`_vllm_disable_chat_template_kwargs()` 根据 `effective_wte` 中已存在的键（`thinking` 或 `enable_thinking`）生成禁用值，确保只覆盖配置中明确使用的字段。

---

## 四、第 3 层：vLLM Provider — reasoning 字段的全生命周期保留

### 4.1 三个覆写点的数据流

```
                    vLLM API 响应
                         │
           ┌─────────────┼─────────────┐
           │             │             │
      非流式响应      流式 chunk     多轮请求
           │             │             │
  _create_chat_result  _convert_chunk  _get_request_payload
           │             │             │
      choice.message   delta.reasoning  │ 恢复 reasoning
      .reasoning       → additional_   │ 到 payload 消息
           │           kwargs.reasoning │
           ▼             │             ▼
  additional_kwargs:     │      payload.messages[*]
    reasoning: 原始值     │        .reasoning = 原始值
    reasoning_content: 文本 │
           │             │             │
           └─────→ AIMessage.additional_kwargs ────→ 下轮 _get_request_payload
```

### 4.2 消息匹配策略

`_get_request_payload()` 需要将原始 `AIMessage` 的 reasoning 字段恢复到序列化后的 payload 消息中。但 LangChain 的 `_convert_input()` 可能改变消息数量（合并、过滤）。

```python
if len(payload_messages) == len(original_messages):
    # 精确匹配：逐位置对应
    for payload_msg, orig_msg in zip(payload_messages, original_messages):
        if payload_msg["role"] == "assistant" and isinstance(orig_msg, AIMessage):
            _restore_reasoning_field(payload_msg, orig_msg)
else:
    # 回退匹配：按角色过滤后位置对应
    ai_messages = [m for m in original_messages if isinstance(m, AIMessage)]
    assistant_payloads = [m for m in payload_messages if m["role"] == "assistant"]
    for payload_msg, ai_msg in zip(assistant_payloads, ai_messages):
        _restore_reasoning_field(payload_msg, ai_msg)
```

### 4.3 reasoning 文本提取

vLLM 的 `reasoning` 字段格式不固定，`_reasoning_to_text()` 递归处理：

| 输入类型 | 处理方式 |
|----------|----------|
| `str` | 直接返回 |
| `list` | 递归处理每个元素后拼接 |
| `dict` | 按 `text` > `content` > `reasoning` 优先级查找 |
| 其他 | `json.dumps()` 回退 |

### 4.4 chat_template_kwargs 归一化

DeerFlow 历史上使用 `thinking` 字段控制 vLLM，但 vLLM 0.19.0 的 Qwen 推理解析器读取 `enable_thinking`。`_normalize_vllm_chat_template_kwargs()` 在每次请求前归一化：

```python
# thinking → enable_thinking（如果 enable_thinking 未设置）
normalized.setdefault("enable_thinking", normalized["thinking"])
normalized.pop("thinking", None)  # 移除旧字段避免冲突
```

---

## 五、配置优先级链

### 5.1 模型参数的 4 层覆盖

```
优先级从高到低：

① kwargs（运行时参数）
   create_chat_model(name, stream_usage=True)
         │
② model_settings_from_config（config.yaml 序列化后的参数）
   api_key, base_url, max_tokens, ...
         │
③ when_thinking_enabled / when_thinking_disabled（条件配置）
   thinking 切换时动态注入的参数
         │
④ Provider 默认值
   类定义中的 Field(default=...)
```

实现：`model_class(**kwargs, **model_settings_from_config)` — kwargs 在前，Python 的参数规则确保 kwargs 同名键优先。

### 5.2 stream_usage 的三层决策

```python
# 1. 用户显式配置（最高优先级）
if "stream_usage" in model_settings_from_config:
    → 使用配置值

# 2. 自定义端点自动启用
if "base_url" in model_settings_from_config:
    → stream_usage = True

# 3. Provider 类支持则默认启用
if "stream_usage" in model_class.model_fields:
    → stream_usage = True
```

---

## 六、凭证加载链

### 6.1 Claude Code OAuth 凭证查找

```
优先级 1: $CLAUDE_CODE_OAUTH_TOKEN / $ANTHROPIC_AUTH_TOKEN
    ↓ 失败
优先级 2: $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR
    ↓ 失败
优先级 3: $CLAUDE_CODE_CREDENTIALS_PATH 指定的文件
    ↓ 失败
优先级 4: ~/.claude/.credentials.json（默认路径）
    ↓ 失败
返回 None → ClaudeChatModel 使用标准 API Key
```

`ClaudeChatModel.model_post_init()` 中检测到 OAuth Token（`sk-ant-oat` 前缀）后，通过 `_patch_client_oauth()` 将 SDK 客户端的 `api_key` 替换为 `auth_token`，实现 Bearer 认证。

### 6.2 Codex CLI 凭证查找

```
$CODEX_AUTH_PATH → ~/.codex/auth.json
    ↓
兼容新旧两种格式：
  旧版: { "access_token": "...", "account_id": "..." }
  新版: { "tokens": { "access_token": "...", "account_id": "..." } }
```

---

## 七、文件职责速查表

| 文件 | 代码行 | 核心职责 | 关键类/函数 |
|------|--------|----------|------------|
| `factory.py` | ~290 | 工厂核心：反射创建、thinking 切换、配置合并 | `create_chat_model()` |
| `claude_provider.py` | ~560 | Claude 增强：OAuth、缓存、思维预算、重试 | `ClaudeChatModel` |
| `vllm_provider.py` | ~440 | vLLM 适配：reasoning 保留、消息匹配 | `VllmChatModel` |
| `openai_codex_provider.py` | ~450 | Codex 适配：Responses API、SSE 流式 | `CodexChatModel` |
| `credential_loader.py` | ~390 | 凭证加载：Claude Code OAuth + Codex CLI | `load_claude_code_credential()` |
| `patched_deepseek.py` | ~120 | DeepSeek 补丁：reasoning_content 保留 | `PatchedChatDeepSeek` |
| `patched_minimax.py` | ~140 | MiniMax 补丁：reasoning_details 解析 | `PatchedChatMiniMax` |
| `patched_openai.py` | ~100 | OpenAI 补丁：thought_signature 保留 | `PatchedChatOpenAI` |
| `mindie_provider.py` | ~350 | MindIE 适配：XML 工具调用、流式回退 | `MindIEChatModel` |

**外部依赖**：

| 文件 | 位置 | 职责 |
|------|------|------|
| `config/model_config.py` | `config/` | `ModelConfig` Pydantic 模型 |
| `config/app_config.py` | `config/` | `AppConfig.get_model_config()` |
| `reflection/__init__.py` | `reflection/` | `resolve_class()` 反射加载 |
| `tracing/__init__.py` | `tracing/` | `build_tracing_callbacks()` |
