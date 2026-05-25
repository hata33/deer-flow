# 完整生命周期

本文档跟踪一个模型从配置文件到实际产生响应的完整生命周期。

## 1. 配置加载阶段

### 1.1 config.yaml 解析

应用启动时，`get_app_config()` 触发配置加载：

```
config.yaml
  → YAML 解析 (yaml.safe_load)
  → 配置版本检查 (与 config.example.yaml 比较)
  → 环境变量解析 ($VAR 语法)
  → Pydantic 校验 (AppConfig.model_validate)
  → 分发到子系统全局单例
```

环境变量解析将 `$OPENAI_API_KEY` 替换为实际值：

```yaml
# config.yaml
models:
  - name: gpt-4o
    api_key: $OPENAI_API_KEY    # → "sk-..."
```

```python
# app_config.py
def resolve_env_variables(cls, config):
    if isinstance(config, str) and config.startswith("$"):
        env_value = os.getenv(config[1:])
        if env_value is None:
            raise ValueError(f"Environment variable {config[1:]} not found")
        return env_value
```

### 1.2 ModelConfig 构建

每个 `models[]` 条目被解析为 `ModelConfig` 实例：

```python
class ModelConfig(BaseModel):
    name: str                       # "claude-sonnet-4.6"
    use: str                        # "deerflow.models.claude_provider:ClaudeChatModel"
    model: str                      # "claude-sonnet-4-6"
    supports_thinking: bool = False # 能力标记
    supports_vision: bool = False   # 能力标记
    when_thinking_enabled: dict     # thinking 开启时的参数
    when_thinking_disabled: dict    # thinking 关闭时的参数
    # ... extra="allow" 允许 Provider 特定字段透传
```

`extra="allow"` 确保所有 Provider 特定字段（如 `enable_prompt_caching`、`base_url`）都能通过 Pydantic 校验。

### 1.3 配置缓存与热更新

```python
def get_app_config():
    # 1. ContextVar 覆盖优先（测试/运行时注入）
    runtime_override = _current_app_config.get()
    if runtime_override is not None:
        return runtime_override

    # 2. 检测文件变更
    current_mtime = _get_config_mtime(resolved_path)
    should_reload = (
        _app_config is None
        or _app_config_path != resolved_path
        or _app_config_mtime != current_mtime
    )

    # 3. 变更时自动重新加载
    if should_reload:
        _load_and_cache_app_config()
```

## 2. 运行时选择阶段

### 2.1 模型名称解析

Lead Agent 在每次请求时从 `RunnableConfig` 中读取模型选择：

```python
# thread_state 中的 configurable 字段
config.configurable = {
    "model_name": "claude-sonnet-4.6",   # 用户选择的模型
    "thinking_enabled": True,             # 是否启用 thinking
    "is_plan_mode": False,                # 是否启用计划模式
    "subagent_enabled": True,             # 是否启用子代理
}
```

### 2.2 create_chat_model 调用

```python
# agents/lead_agent/agent.py
model = create_chat_model(
    name=config["configurable"].get("model_name"),
    thinking_enabled=config["configurable"].get("thinking_enabled", False),
)
```

## 3. 凭据解析阶段

凭据解析发生在 Provider 的 `model_post_init()` 中，即 `create_chat_model()` 调用 `model_class(**settings)` 之后。

### 3.1 Claude 凭据解析链

```
ClaudeChatModel.model_post_init()
  │
  ├─ 检查 anthropic_api_key
  │    ├─ 有效值 → 直接使用（API Key 模式）
  │    └─ 无效/缺失 → 进入 OAuth 查找
  │
  ├─ load_claude_code_credential()
  │    ├─ $CLAUDE_CODE_OAUTH_TOKEN / $ANTHROPIC_AUTH_TOKEN
  │    ├─ $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR
  │    ├─ $CLAUDE_CODE_CREDENTIALS_PATH
  │    └─ ~/.claude/.credentials.json
  │
  ├─ is_oauth_token(key)?
  │    ├─ Yes → OAuth Bearer 模式
  │    │    ├─ _patch_client_oauth()：swap api_key → auth_token
  │    │    ├─ 添加 anthropic-beta headers
  │    │    └─ 禁用 prompt caching（OAuth 限制）
  │    └─ No → 标准 API Key 模式
  │
  └─ super().model_post_init()  # ChatAnthropic 初始化
```

### 3.2 Codex 凭据解析链

```
CodexChatModel.model_post_init()
  │
  ├─ _load_codex_auth()
  │    └─ load_codex_cli_credential()
  │         ├─ $CODEX_AUTH_PATH 指定的文件
  │         └─ ~/.codex/auth.json
  │
  ├─ 凭据存在 → 设置 _access_token, _account_id
  └─ 凭据不存在 → raise ValueError
```

### 3.3 标准凭据（OpenAI/vLLM/DeepSeek/MiniMax/MindIE）

这些 Provider 直接使用配置中的 `api_key` 字段（已在环境变量解析阶段替换为实际值），无需额外查找：

```yaml
api_key: $OPENAI_API_KEY    # 已在加载时替换为 "sk-..."
```

## 4. Provider 实例化阶段

```python
# factory.py
model_class = resolve_class(model_config.use, BaseChatModel)
# 例如 resolve_class("deerflow.models.claude_provider:ClaudeChatModel", BaseChatModel)
# → 动态导入 deerflow.models.claude_provider
# → 获取 ClaudeChatModel 类
# → 验证 isinstance(ClaudeChatModel, type) and issubclass(ClaudeChatModel, BaseChatModel)

model_instance = model_class(**kwargs, **model_settings_from_config)
# → 触发 model_post_init()
# → 凭据加载（如上述）
# → 客户端创建
```

反射过程：

```
"deerflow.models.claude_provider:ClaudeChatModel"
  ↓ 分割为 (module_path, class_name)
  ↓ importlib.import_module(module_path)
  ↓ getattr(module, class_name)
  ↓ 验证 issubclass(cls, BaseChatModel)
  ✓ 返回类对象
```

## 5. Thinking 切换阶段

Thinking 的启用/禁用在 `create_chat_model()` 中通过配置合并实现，而非运行时动态切换。每次创建模型实例时，thinking 状态就已经固化在实例参数中。

### 5.1 启用 Thinking

```yaml
# config.yaml
models:
  - name: claude-sonnet-4.6
    supports_thinking: true
    thinking:
      type: enabled
    max_tokens: 16384
```

合并后实际传给构造函数的参数：

```python
{
    "model": "claude-sonnet-4-6",
    "max_tokens": 16384,
    "thinking": {"type": "enabled"},  # 从 thinking 字段合并而来
}
```

`ClaudeChatModel._apply_thinking_budget()` 进一步处理：

```python
# payload 中的 thinking 变为：
{
    "type": "enabled",
    "budget_tokens": 13107  # 16384 * 0.8
}
```

### 5.2 禁用 Thinking

不同 Provider 的禁用路径：

| Provider | 禁用方式 |
|----------|----------|
| Claude（原生） | `thinking: {"type": "disabled"}` |
| OpenAI 兼容网关 | `extra_body.thinking.type = "disabled"` + `reasoning_effort = "minimal"` |
| vLLM Qwen | `extra_body.chat_template_kwargs.enable_thinking = False` |
| Codex | `reasoning_effort = "none"` |
| DeepSeek | 不传 thinking 相关参数（`reasoning_content` 不出现在后续请求中） |

### 5.3 vLLM thinking 归一化

```
配置：thinking: true
  → factory 合并到 when_thinking_enabled.extra_body.chat_template_kwargs.thinking
  → VllmChatModel._get_request_payload() 中归一化
  → thinking → enable_thinking（vLLM 0.19.0 字段名）
```

## 6. Prompt Caching 阶段（仅 Claude）

```
_get_request_payload()
  │
  └─ _apply_prompt_caching()
       │
       ├─ 收集候选块（system + 最近 3 条消息 + 最后一个工具定义）
       │
       ├─ 取最后 4 个候选块
       │
       └─ 设置 cache_control: {type: "ephemeral"}
```

缓存断点布局示例：

```
[system prompt block]           ← cache_control（断点 1）
  ... 
[message -3: human]             ← cache_control（断点 2）
[message -2: assistant]         ← cache_control（断点 3）
[message -1: human]             ← cache_control（断点 4）
[tools: last tool definition]   （超过 4 个断点限制，不设置）
```

**注意**：OAuth 模式下，`_create()` 会在发送前调用 `_strip_cache_control()` 移除所有缓存标记。

## 7. 请求发送阶段

### 7.1 _get_request_payload 覆写链

不同 Provider 在 `_get_request_payload()` 中执行不同的 payload 修改：

```
ClaudeChatModel._get_request_payload()
  ├─ super()._get_request_payload()   # 标准 ChatAnthropic payload
  ├─ _apply_oauth_billing()           # [OAuth] 注入 billing header + metadata.user_id
  ├─ _apply_prompt_caching()          # [非 OAuth] 放置 cache_control 断点
  └─ _apply_thinking_budget()         # 自动分配 thinking budget

VllmChatModel._get_request_payload()
  ├─ super()._get_request_payload()   # 标准 ChatOpenAI payload
  ├─ _normalize_vllm_chat_template_kwargs()  # thinking → enable_thinking
  └─ _restore_reasoning_field()       # 恢复 assistant 消息的 reasoning 字段

PatchedChatDeepSeek._get_request_payload()
  ├─ super()._get_request_payload()
  └─ 恢复 assistant 消息的 reasoning_content

PatchedChatOpenAI._get_request_payload()
  ├─ super()._get_request_payload()
  └─ _restore_tool_call_signatures()  # 恢复 thought_signature

MindIEChatModel._generate()
  └─ _fix_messages(messages)          # 消息格式转换（XML 工具调用）
```

### 7.2 Codex 的独立路径

`CodexChatModel` 不使用 `_get_request_payload()`，而是直接在 `_call_codex_api()` 中构建请求：

```python
def _call_codex_api(self, messages, tools=None):
    instructions, input_items = self._convert_messages(messages)
    payload = {
        "model": self.model,
        "instructions": instructions,
        "input": input_items,
        "store": False,
        "stream": True,
        "reasoning": {"effort": self.reasoning_effort, "summary": "detailed"},
    }
    if tools:
        payload["tools"] = self._convert_tools(tools)

    # 通过 httpx 发送 SSE 请求
    return self._stream_response(headers, payload)
```

## 8. 响应处理阶段

### 8.1 非流式响应

标准路径：

```
Provider._generate(messages)
  → super()._generate()          # LangChain 标准生成
  → Provider 特定后处理
```

| Provider | 后处理 |
|----------|--------|
| ClaudeChatModel | 重试 RateLimitError / InternalServerError |
| VllmChatModel | 保留 `reasoning` → `additional_kwargs` |
| MindIEChatModel | `_patch_result_with_tools()`：XML 解析 + 转义修复 |
| CodexChatModel | `_parse_response()`：从 Responses API 提取 content + tool_calls |

### 8.2 流式响应

标准路径：

```
Provider._astream(messages)
  → super()._astream()           # LangChain 标准流式生成
  → Provider._convert_chunk_to_generation_chunk()  # 逐 chunk 处理
```

各 Provider 的流式处理：

| Provider | 流式处理 |
|----------|----------|
| VllmChatModel | 自定义 `_convert_delta_to_message_chunk_with_reasoning()`，保留 `reasoning` 字段 |
| PatchedChatMiniMax | 在 chunk 中提取 `reasoning_details`，映射到 `reasoning_content` |
| MindIEChatModel | 无工具时正常流式 + 转义修复；有工具时回退到非流式 + 模拟分块 |

### 8.3 MindIE 流式回退

```
_astream(messages, tools=tools)
  │
  ├─ 无工具
  │    └─ super()._astream(_fix_messages(messages))
  │         └─ 逐 chunk: _decode_escaped_newlines_outside_fences()
  │
  └─ 有工具（MindIE 流式会丢失 choices）
       └─ await self._agenerate(messages, tools=tools)
            └─ 模拟流式：按 15 字符分块输出
                 ├─ AIMessageChunk(content=chunk_text)
                 └─ 最后一个 chunk 携带 tool_calls
```

## 9. 错误恢复阶段

### 9.1 重试机制

只有两个 Provider 实现了自动重试：

**ClaudeChatModel**：

```python
# 重试条件：RateLimitError 或 InternalServerError
# 最大重试：MAX_RETRIES = 3
# 退避策略：指数退避 + 20% jitter + Retry-After 支持

for attempt in range(1, self.retry_max_attempts + 1):
    try:
        return super()._generate(messages, stop=stop, **kwargs)
    except anthropic.RateLimitError as e:
        if attempt >= self.retry_max_attempts:
            raise
        wait_ms = self._calc_backoff_ms(attempt, e)
        time.sleep(wait_ms / 1000)
```

**CodexChatModel**：

```python
# 重试条件：HTTP 429、500、529
# 最大重试：MAX_RETRIES = 3
# 退避策略：2s × 2^(attempt-1)（无 jitter）

for attempt in range(1, self.retry_max_attempts + 1):
    try:
        return self._stream_response(headers, payload)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (429, 500, 529):
            if attempt >= self.retry_max_attempts:
                raise
            wait_ms = 2000 * (1 << (attempt - 1))
            time.sleep(wait_ms / 1000)
        else:
            raise  # 非 429/500/529 错误直接抛出
```

### 9.2 MindIE 兼容性处理

MindIE 的错误不是通过重试解决的，而是通过预防性兼容：

| 问题 | 预防措施 |
|------|----------|
| 多模态 content 导致 0-token 生成 | `_fix_messages()` 扁平化为字符串 |
| XML 工具调用不被识别 | `_parse_xml_tool_call_to_dict()` 解析为标准格式 |
| 流式 + 工具丢失 choices | 回退到非流式生成 |
| 过度转义的 `\n` | `_decode_escaped_newlines_outside_fences()` 修复 |

### 9.3 上层错误处理

在 Provider 之外，DeerFlow 还有多层错误处理：

1. **LLMErrorHandlingMiddleware**：捕获 Provider 异常，转换为可恢复的错误消息
2. **ToolErrorHandlingMiddleware**：捕获工具执行异常，转换为错误 ToolMessage
3. **熔断器（CircuitBreaker）**：连续失败达到阈值后熔断，防止持续调用不可用的 LLM

## 端到端流程总结

```
用户请求
  │
  ├─ 1. LangGraph 接收请求，提取 configurable（model_name, thinking_enabled）
  │
  ├─ 2. create_chat_model(name, thinking_enabled)
  │     ├─ 获取 AppConfig（缓存 + mtime 检测）
  │     ├─ 查找 ModelConfig
  │     ├─ resolve_class() → Provider 类
  │     ├─ 合并 thinking 参数
  │     ├─ Provider 特殊处理
  │     └─ 实例化 Provider（触发凭据加载 + 客户端创建）
  │
  ├─ 3. Agent 调用 model.invoke(messages) / model.stream(messages)
  │     ├─ _get_request_payload() 构建 API payload
  │     │     ├─ [Claude] OAuth billing + prompt caching + thinking budget
  │     │     ├─ [vLLM] reasoning 恢复 + chat_template_kwargs 归一化
  │     │     ├─ [DeepSeek] reasoning_content 恢复
  │     │     ├─ [MiniMax] reasoning_split 启用
  │     │     ├─ [Gemini] thought_signature 恢复
  │     │     └─ [MindIE] 消息格式转换为 XML
  │     │
  │     ├─ 发送 HTTP 请求到 LLM API
  │     │
  │     └─ 处理响应
  │           ├─ 非流式：_create_chat_result() 后处理
  │           ├─ 流式：_convert_chunk_to_generation_chunk() 逐 chunk 处理
  │           └─ [MindIE 有工具时] 回退到非流式 + 模拟分块
  │
  └─ 4. 返回标准化消息（BaseMessage / AIMessage）
        ├─ content: 响应文本
        ├─ tool_calls: 结构化工具调用（如有）
        ├─ additional_kwargs.reasoning_content: 推理过程（如启用 thinking）
        └─ usage_metadata: token 统计
```
