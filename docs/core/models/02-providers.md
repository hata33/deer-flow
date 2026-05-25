# 各 Provider 实现

本文档详细描述 models/ 模块中每个 Provider 的实现细节、设计动机和关键代码路径。

## ClaudeChatModel

**文件**：`models/claude_provider.py`
**父类**：`langchain_anthropic.ChatAnthropic`
**配置路径**：`deerflow.models.claude_provider:ClaudeChatModel`

### 概述

`ClaudeChatModel` 是 Claude 系列模型的增强适配器，在标准 `ChatAnthropic` 基础上增加了三大能力：

1. **OAuth Bearer 认证**：支持 Claude Code CLI 的 OAuth token
2. **Prompt Caching**：自动在 system prompt、最近消息和工具定义上放置缓存断点
3. **自动 Thinking Budget**：根据 `max_tokens` 自动分配 80% 给 thinking

### 认证模式

支持两种认证方式：

| 模式 | 认证头 | 触发条件 |
|------|--------|----------|
| API Key | `x-api-key: sk-ant-...` | 标准 API Key（非 `sk-ant-oat` 前缀） |
| OAuth | `Authorization: Bearer sk-ant-oat...` | token 以 `sk-ant-oat` 开头 |

OAuth 模式的配置变更：

```python
if is_oauth_token(current_key):
    self._is_oauth = True
    self._oauth_access_token = current_key
    # 添加 OAuth beta headers
    self.default_headers = {
        **(self.default_headers or {}),
        "anthropic-beta": OAUTH_ANTHROPIC_BETAS,
    }
    # OAuth token 限制 4 个 cache_control 块 → 禁用 prompt caching
    self.enable_prompt_caching = False
```

### Prompt Caching 策略

`_apply_prompt_caching()` 在请求 payload 中放置 `cache_control: {type: "ephemeral"}` 断点，遵循 Anthropic 的硬限制（最多 4 个断点）。

断点放置策略（从文档顺序收集候选块，然后对**最后 4 个**放置断点）：

```
候选块收集顺序：
  1. system text blocks（静态系统提示）
  2. 最近 prompt_cache_size 条消息的 content blocks（默认 3 条）
  3. 最后一个工具定义

断点放置：
  取最后 4 个候选块 → 设置 cache_control
```

选择最后 4 个而非前 4 个，是因为后置断点覆盖更大的前缀，缓存命中率更高。

### Thinking Budget 自动分配

```python
THINKING_BUDGET_RATIO = 0.8

def _apply_thinking_budget(self, payload):
    thinking = payload.get("thinking")
    if thinking and thinking.get("type") == "enabled" and not thinking.get("budget_tokens"):
        max_tokens = payload.get("max_tokens", 8192)
        thinking["budget_tokens"] = int(max_tokens * THINKING_BUDGET_RATIO)
```

仅在 `thinking.type = "enabled"` 且未显式设置 `budget_tokens` 时生效。

### OAuth Billing Header

OAuth 请求需要在 system prompt 的第一个块注入 billing header：

```python
_DEFAULT_BILLING_HEADER = (
    "x-anthropic-billing-header: cc_version=2.1.85.351; "
    "cc_entrypoint=cli; cch=6c6d5;"
)
```

同时需要 `metadata.user_id`（基于 hostname 的 SHA-256 生成稳定 device_id）。

### 重试机制

```python
MAX_RETRIES = 3

# 重试条件：RateLimitError 或 InternalServerError
# 退避策略：2s × 2^(attempt-1) × 1.2 jitter
# 支持 Retry-After header
```

退避公式：

```
backoff_ms = 2000 * (1 << (attempt - 1))
jitter_ms = backoff_ms * 0.2
total_ms = backoff_ms + jitter_ms
```

| 尝试 | 等待时间 |
|------|----------|
| 1 | 2400ms |
| 2 | 4800ms |
| 3 | 9600ms |

### 请求处理链

```
_get_request_payload()
  ├─ super()._get_request_payload()    # 标准 ChatAnthropic payload
  ├─ _apply_oauth_billing()            # OAuth: 注入 billing header
  ├─ _apply_prompt_caching()           # 放置 cache_control 断点
  └─ _apply_thinking_budget()          # 自动分配 thinking budget
```

对于 OAuth 模式，`_create()` 和 `_acreate()` 会在发送前调用 `_strip_cache_control()` 移除所有缓存标记（因为 OAuth 端点不支持 caching）。

### 配置示例

```yaml
- name: claude-sonnet-4.6
  use: deerflow.models.claude_provider:ClaudeChatModel
  model: claude-sonnet-4-6
  max_tokens: 16384
  enable_prompt_caching: true
  supports_thinking: true
  thinking:
    type: enabled
```

---

## credential_loader

**文件**：`models/credential_loader.py`

### 概述

凭据加载器为 Claude Code CLI 和 Codex CLI 提供统一的凭据发现和加载机制。它不创建 Provider 类，而是作为 `ClaudeChatModel` 和 `CodexChatModel` 的凭据来源。

### Claude Code OAuth 凭据

`load_claude_code_credential()` 按以下顺序查找凭据：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | `$CLAUDE_CODE_OAUTH_TOKEN` 或 `$ANTHROPIC_AUTH_TOKEN` | 环境变量直接传递 |
| 2 | `$CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` | 文件描述符（进程间安全传递） |
| 3 | `$CLAUDE_CODE_CREDENTIALS_PATH` | 自定义凭据文件路径 |
| 4 | `~/.claude/.credentials.json` | 默认凭据文件 |

凭据文件格式：

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1773430695128,
    "scopes": ["user:inference", ...]
  }
}
```

#### 文件描述符读取

```python
def _read_secret_from_file_descriptor(env_var: str) -> str | None:
    fd_value = os.getenv(env_var)
    fd = int(fd_value)
    secret = os.read(fd, 1024 * 1024).decode().strip()
    return secret or None
```

通过文件描述符传递凭据是一种安全实践，避免在命令行参数或环境变量中暴露敏感信息。

#### 过期检查

```python
@property
def is_expired(self) -> bool:
    if self.expires_at <= 0:
        return False
    return time.time() * 1000 > self.expires_at - 60_000  # 1 分钟缓冲
```

使用毫秒级时间戳比较，提前 1 分钟判定过期，留出刷新窗口。

### Codex CLI 凭据

`load_codex_cli_credential()` 从 `~/.codex/auth.json` 加载凭据：

```python
def load_codex_cli_credential() -> CodexCliCredential | None:
    cred_path = _resolve_credential_path("CODEX_AUTH_PATH", ".codex/auth.json")
    data = _load_json_file(cred_path, "Codex CLI credentials")
    # 支持多种 JSON 结构：
    # 1. { "access_token": "...", "account_id": "..." }
    # 2. { "token": "...", "account_id": "..." }
    # 3. { "tokens": { "access_token": "...", "account_id": "..." } }
```

---

## VllmChatModel

**文件**：`models/vllm_provider.py`
**父类**：`langchain_openai.ChatOpenAI`
**配置路径**：`deerflow.models.vllm_provider:VllmChatModel`

### 概述

vLLM 0.19.0 通过 OpenAI 兼容 API 暴露 reasoning 模型，但 LangChain 的 `ChatOpenAI` 会丢弃非标准的 `reasoning` 字段。`VllmChatModel` 解决了这个问题。

### 核心问题

vLLM 在 assistant 消息中返回 `reasoning` 字段（包含推理过程），并在多轮对话中要求回传该字段。标准 `ChatOpenAI` 会丢弃它，导致后续请求出错。

### 解决方案

覆写三个关键方法：

#### _get_request_payload — 请求时恢复 reasoning

```python
def _get_request_payload(self, input_, *, stop=None, **kwargs):
    original_messages = self._convert_input(input_).to_messages()
    payload = super()._get_request_payload(input_, stop=stop, **kwargs)
    _normalize_vllm_chat_template_kwargs(payload)
    # 按 position 匹配 assistant 消息，恢复 reasoning 字段
    for payload_msg, orig_msg in zip(payload_messages, original_messages):
        if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
            _restore_reasoning_field(payload_msg, orig_msg)
    return payload
```

#### _create_chat_result — 非流式响应保留 reasoning

```python
def _create_chat_result(self, response, generation_info=None):
    result = super()._create_chat_result(response, generation_info)
    for generation, choice in zip(result.generations, response_dict.get("choices", [])):
        reasoning = choice.get("message", {}).get("reasoning")
        if reasoning:
            message.additional_kwargs["reasoning"] = reasoning
            message.additional_kwargs["reasoning_content"] = _reasoning_to_text(reasoning)
    return result
```

#### _convert_chunk_to_generation_chunk — 流式 delta 保留 reasoning

```python
def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
    # 使用自定义 _convert_delta_to_message_chunk_with_reasoning
    message_chunk = _convert_delta_to_message_chunk_with_reasoning(choice["delta"], default_chunk_class)
```

### chat_template_kwargs 归一化

```python
def _normalize_vllm_chat_template_kwargs(payload):
    # 旧配置使用 thinking，新 vLLM 使用 enable_thinking
    # 自动将 thinking → enable_thinking
    if "thinking" in chat_template_kwargs:
        normalized_chat_template_kwargs.setdefault("enable_thinking", ...)
        normalized_chat_template_kwargs.pop("thinking", None)
```

### 配置示例

```yaml
- name: qwen-r1
  use: deerflow.models.vllm_provider:VllmChatModel
  model: Qwen/Qwen3-235B-A22B
  base_url: http://localhost:8000/v1
  api_key: token-abc123
  supports_thinking: true
  when_thinking_enabled:
    extra_body:
      chat_template_kwargs:
        enable_thinking: true
```

---

## CodexChatModel

**文件**：`models/openai_codex_provider.py`
**父类**：`langchain_core.language_models.chat_models.BaseChatModel`
**配置路径**：`deerflow.models.openai_codex_provider:CodexChatModel`

### 概述

`CodexChatModel` 完全不使用 `ChatOpenAI`，而是直接实现 `BaseChatModel`，通过 Codex Responses API（`chatgpt.com/backend-api/codex/responses`）与 ChatGPT Codex 端点通信。

### 与其他 Provider 的区别

| 特性 | 其他 Provider | CodexChatModel |
|------|---------------|----------------|
| 基类 | `ChatOpenAI` / `ChatAnthropic` | `BaseChatModel` |
| API 格式 | Chat Completions | Responses API |
| 端点 | 各 Provider API | `chatgpt.com/backend-api/codex/responses` |
| 认证 | API Key | Codex CLI OAuth token |
| 流式 | 可选 | 必须启用（`stream: true`） |

### 消息格式转换

LangChain 消息格式转换为 Codex Responses API 格式：

```python
def _convert_messages(self, messages):
    # SystemMessage → instructions（合并所有 system 消息）
    # HumanMessage → { role: "user", content: "..." }
    # AIMessage → { role: "assistant", content: "..." }
    #   + tool_calls → { type: "function_call", name, arguments, call_id }
    # ToolMessage → { type: "function_call_output", call_id, output }
```

### SSE 流式处理

```python
def _stream_response(self, headers, payload):
    # 消费 SSE 事件流：
    # - response.output_item.done → 缓存 output items
    # - response.completed → 完成信号
    # 最终合并缓存 items 和 completed response
```

### 响应解析

```python
def _parse_response(self, response):
    # 遍历 output items：
    # - type: "reasoning" → reasoning_content
    # - type: "message" → content text
    # - type: "function_call" → tool_calls
```

### 重试策略

```python
# 仅对 429、500、529 错误重试
# 退避：2s × 2^(attempt-1)
# 最多 3 次
```

### 配置示例

```yaml
- name: gpt-5.4
  use: deerflow.models.openai_codex_provider:CodexChatModel
  model: gpt-5.4
  reasoning_effort: medium
```

---

## PatchedChatDeepSeek

**文件**：`models/patched_deepseek.py`
**父类**：`langchain_deepseek.ChatDeepSeek`

### 核心问题

DeepSeek API 在 thinking 模式启用时，要求所有 assistant 消息都携带 `reasoning_content`。但 LangChain 的 `ChatDeepSeek` 将 `reasoning_content` 存储在 `additional_kwargs` 中，发送请求时不会回传，导致多轮对话 API 报错。

### 解决方案

覆写 `_get_request_payload()`，在发送前将 `reasoning_content` 从 `additional_kwargs` 恢复到 payload 中：

```python
def _get_request_payload(self, input_, *, stop=None, **kwargs):
    original_messages = self._convert_input(input_).to_messages()
    payload = super()._get_request_payload(input_, stop=stop, **kwargs)

    for payload_msg, orig_msg in zip(payload_messages, original_messages):
        if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
            reasoning_content = orig_msg.additional_kwargs.get("reasoning_content")
            if reasoning_content is not None:
                payload_msg["reasoning_content"] = reasoning_content
    return payload
```

支持两种匹配策略：
1. **等长匹配**：payload 消息数 = 原始消息数 → 逐位置配对
2. **角色匹配**：只配对 assistant 角色的消息

---

## PatchedChatMiniMax

**文件**：`models/patched_minimax.py`
**父类**：`langchain_openai.ChatOpenAI`

### 核心问题

MiniMax API 通过 `reasoning_details` 字段返回推理内容，并且可能在正文中嵌入 `<think` 标签。标准 `ChatOpenAI` 不识别这些字段。

### 解决方案

#### 请求端：启用 reasoning_split

```python
def _get_request_payload(self, input_, *, stop=None, **kwargs):
    payload = super()._get_request_payload(input_, stop=stop, **kwargs)
    payload["extra_body"] = {**extra_body, "reasoning_split": True}
    return payload
```

#### 响应端：解析 reasoning_details

```python
def _create_chat_result(self, response, generation_info=None):
    # 1. 从 reasoning_details 提取推理文本
    # 2. 从正文剥离 <think`...</think`> 标签
    # 3. 合并两种来源的推理内容
    # 4. 存入 additional_kwargs.reasoning_content
```

#### 流式端：chunk 级别处理

覆写 `_convert_chunk_to_generation_chunk()` 在流式 delta 中保留 `reasoning_details`。

---

## PatchedChatOpenAI

**文件**：`models/patched_openai.py`
**父类**：`langchain_openai.ChatOpenAI`

### 核心问题

通过 OpenAI 兼容网关使用 Gemini thinking 模型时，API 在 tool-call 对象上返回 `thought_signature`，并要求在后续请求中原样回传。但 LangChain 序列化时会丢弃这个非标准字段，导致 API 返回 400 错误：

```
Unable to submit request because function call `<tool>` in the N. content
block is missing a `thought_signature`.
```

### 解决方案

覆写 `_get_request_payload()`，从 `additional_kwargs["tool_calls"]` 中恢复 `thought_signature`：

```python
def _get_request_payload(self, input_, *, stop=None, **kwargs):
    original_messages = self._convert_input(input_).to_messages()
    payload = super()._get_request_payload(input_, stop=stop, **kwargs)
    for payload_msg, orig_msg in zip(payload_messages, original_messages):
        if payload_msg.get("role") == "assistant" and isinstance(orig_msg, AIMessage):
            _restore_tool_call_signatures(payload_msg, orig_msg)
    return payload
```

匹配策略：优先按 `id` 匹配，回退到位置匹配。同时支持 `thought_signature` 和 `thoughtSignature`（camelCase）。

### 配置示例

```yaml
- name: gemini-2.5-pro-thinking
  use: deerflow.models.patched_openai:PatchedChatOpenAI
  model: google/gemini-2.5-pro-preview
  api_key: $GEMINI_API_KEY
  base_url: https://<gateway>/v1
  supports_thinking: true
  supports_vision: true
  when_thinking_enabled:
    extra_body:
      thinking:
        type: enabled
```

---

## MindIEChatModel

**文件**：`models/mindie_provider.py`
**父类**：`langchain_openai.ChatOpenAI`

### 概述

MindIE 是华为昇腾 NPU 的推理引擎。它的 OpenAI 兼容层存在多种兼容性问题，`MindIEChatModel` 逐一解决。

### 兼容性问题与解决方案

#### 1. 消息格式兼容（_fix_messages）

MindIE 的 chat template 无法解析 LangChain 的原生 `tool_calls` 和 `ToolMessage`：

```python
def _fix_messages(messages):
    # AIMessage with tool_calls → 转换为 XML 格式
    #   <tool_call`><function=name> <parameter=key>value</parameter`> </function`></tool_call`>
    # ToolMessage → 转换为 HumanMessage，包裹在 <tool_output`> 标签中
```

#### 2. XML 工具调用解析（_parse_xml_tool_call_to_dict）

模型输出的工具调用是 XML 格式，需要解析为 LangChain 标准的 `tool_calls`：

```python
def _parse_xml_tool_call_to_dict(content):
    # 解析 <tool_call`><function=name> ... </function`></tool_call`> 块
    # 提取参数值，尝试 JSON 反序列化
    # 返回 (cleaned_text, tool_calls_list)
```

支持嵌套 `<tool_call`> 块（多个并行工具调用）和参数值的自动类型推断。

#### 3. 流式工具调用回退

MindIE 在 `stream=True` 且存在 tools 时会丢弃 choices。解决方案是回退到非流式生成，然后模拟流式输出：

```python
async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
    if not kwargs.get("tools"):
        # 无工具：正常流式
        async for chunk in super()._astream(...):
            yield chunk
        return

    # 有工具：回退到非流式，然后模拟分块流式输出
    result = await self._agenerate(messages, ...)
    for gen in result.generations:
        # 按 15 字符分块输出
        for i in range(0, len(content), 15):
            yield ChatGenerationChunk(message=AIMessageChunk(content=chunk_text))
```

#### 4. 转义换行修复

```python
def _decode_escaped_newlines_outside_fences(content):
    # 将字面 \\n 转换为实际换行，但保留 fenced code blocks 内的 \\n
    parts = re.split(r"(```[\s\S]*?```)", content)
    for idx, part in enumerate(parts):
        if not part.startswith("```"):
            parts[idx] = part.replace("\\n", "\n")
    return "".join(parts)
```

#### 5. 超时归一化

```python
def __init__(self, **kwargs):
    connect_timeout = kwargs.pop("connect_timeout", 30.0)
    read_timeout = kwargs.pop("read_timeout", 900.0)
    write_timeout = kwargs.pop("write_timeout", 60.0)
    pool_timeout = kwargs.pop("pool_timeout", 30.0)
    kwargs.setdefault("timeout", httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    ))
```

将分散的超时参数归一化为 `httpx.Timeout`，默认 read timeout 为 900s（适配 NPU 推理的长延迟）。

## Provider 能力矩阵

| Provider | Thinking | Vision | Tool Calling | 流式 | 特殊认证 |
|----------|----------|--------|-------------|------|----------|
| ClaudeChatModel | 原生 | 支持 | 支持 | 支持 | OAuth Bearer |
| VllmChatModel | chat_template_kwargs | 取决于模型 | 支持 | 支持 | API Key |
| CodexChatModel | reasoning_effort | 不支持 | 支持 | 必须 | Codex CLI OAuth |
| PatchedChatDeepSeek | reasoning_content | 不支持 | 支持 | 支持 | API Key |
| PatchedChatMiniMax | reasoning_details | 不支持 | 支持 | 支持 | API Key |
| PatchedChatOpenAI | thought_signature | 支持 | 支持 | 支持 | API Key |
| MindIEChatModel | 不支持 | 不支持 | XML 解析 | 回退 | API Key |
