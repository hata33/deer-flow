# 003-模型工厂

## 解决什么问题

项目需要对接多家模型提供商（Anthropic、OpenAI、DeepSeek、MiniMax、Codex），每家的认证方式、API 协议、thinking 处理、流式格式各不相同。
上层调用方（Agent 工厂）不应关心这些差异，只传模型名 + 能力参数，拿到 `BaseChatModel` 实例即可。

## 本模块的职责边界

**只负责模型实例化**：配置查找 → 反射加载类 → 参数合并 → 能力适配 → 凭证注入 → 追踪附加。
不负责：配置文件加载（配置系统模块）、工具选择（工具模块）、模型调用编排（Agent Loop）。

## 不可变的设计决策

**反射加载而非硬编码 if-else**：`resolve_class("langchain_openai:ChatOpenAI")` 从配置字符串动态导入。
新增模型提供商只需在 config.yaml 加一行 `use`，不改工厂代码。代价是启动时才能发现拼写错误——`_build_missing_dependency_hint` 给出可操作的安装提示。

**序列化排除元数据字段**：`model_dump(exclude={"use","name","supports_thinking",...})` 过滤掉 `ModelConfig` 的元数据字段。
不过滤的话这些字段传给 `ChatOpenAI(**kwargs)` 会报 `TypeError: unexpected keyword argument`。

**thinking 快捷方式深度合并**：`thinking` 是 `when_thinking_enabled["thinking"]` 的简写。合并规则：快捷方式值覆盖默认值，但保留 `when_thinking_enabled` 中的非 thinking 字段（如 `extra_body`）。浅合并会丢失嵌套字段。

**thinking 禁用必须显式**：Anthropic SDK 省略 `thinking` 参数不等于禁用。工厂在 `thinking_enabled=False` 时显式注入 `{"thinking": {"type": "disabled"}}`，区分两种嵌套位置（`extra_body` 下 vs 直接参数）。

**reasoning_effort 不支持时静默移除**：`del kwargs["reasoning_effort"]` 而非报错。用户可能配了全局 reasoning_effort，但不是所有模型都支持——不应让配置驱动全局参数导致部分模型报错。

**Codex 端点特殊映射**：`CodexChatModel` 不接受 `max_tokens`，工厂 `pop` 掉它；thinking 模式映射为 `reasoning_effort`（none/medium/high/xhigh），因为 Codex Responses API 没有 thinking 参数。

**kwargs 在前 config 在后**：`model_class(**kwargs, **model_settings_from_config)`。运行时参数（如 `reasoning_effort`）覆盖配置文件参数。顺序反了则运行时无法覆盖配置。

**Provider 类继承而非包装**：`ClaudeChatModel(ChatAnthropic)` 继承父类，在 `model_post_init` 钩子中加载凭证、在 `_get_request_payload` 中注入缓存/预算。不用包装器（wrapper）是因为包装器无法透传 LangChain 的 `bind_tools`、`astream` 等全部方法。

**Patched 类修 SDK 的 bug**：`PatchedChatOpenAI` 修复 Gemini 网关丢弃 `thought_signature`；`PatchedChatDeepSeek` 修复多轮对话 `reasoning_content` 丢失；`PatchedChatMiniMax` 注入 `reasoning_split` 并映射 `reasoning_details`。这些都是上游 SDK 的已知问题，等官方修复不现实。

**凭证加载链式降级**：Claude 支持 4 级来源（环境变量 → 文件描述符 → 自定义路径 → 默认路径），Codex 从 `~/.codex/auth.json` 加载。`model_post_init` 自动发现——用户不需要手动配置认证。

**追踪附加在最后且不阻断**：`is_tracing_enabled()` 后注入 `LangChainTracer`，失败只 warning 不 raise。追踪是可观测性设施，不应让它的故障阻断模型调用。

## 适配层

```yaml
<ADAPT>
# === 框架 ===
chat_model_base: "BaseChatModel"            # LangChain 基类
reflection_fn: "resolve_class(path, base)"   # 反射函数
config_fn: "get_app_config() -> AppConfig"   # 配置获取函数

# === 模型提供商（按需启用）===
providers:
  - name: "claude"
    class: "ClaudeChatModel"
    features: ["oauth", "prompt_caching", "thinking_budget", "rate_limit_retry"]
  - name: "openai"
    class: "ChatOpenAI"
    features: ["thinking_via_extra_body"]
  - name: "codex"
    class: "CodexChatModel"
    features: ["responses_api", "sse_streaming", "codex_cli_auth"]
  - name: "deepseek"
    class: "PatchedChatDeepSeek"
    features: ["reasoning_content_fix"]
  - name: "minimax"
    class: "PatchedChatMiniMax"
    features: ["reasoning_split", "reasoning_details_mapping"]
  - name: "gemini_gateway"
    class: "PatchedChatOpenAI"
    features: ["thought_signature_preservation"]

# === 能力字段 ===
capability_fields:
  - "supports_thinking"
  - "supports_reasoning_effort"
  - "supports_vision"
  - "use_responses_api"

# === 追踪 ===
tracing_fn: "is_tracing_enabled() -> bool"
tracing_config_fn: "get_tracing_config() -> TracingConfig"
tracer_class: "LangChainTracer"

# === 凭证（按需启用）===
credential_sources:
  claude: ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
           "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
           "CLAUDE_CODE_CREDENTIALS_PATH", "~/.claude/.credentials.json"]
  codex: ["CODEX_AUTH_PATH", "~/.codex/auth.json"]
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | name=None | 降级到 `config.models[0].name` |
| 2 | name 不在配置中 | ValueError |
| 3 | thinking=true + supports_thinking=false | ValueError |
| 4 | thinking=false + 有 thinking 配置 | 显式注入 disabled |
| 5 | thinking=true + 有 thinking + `thinking` 快捷方式 | 深度合并，快捷方式覆盖默认但保留其他字段 |
| 6 | reasoning_effort + supports_reasoning_effort=false | 静默移除，不报错 |
| 7 | Codex 模型 + thinking=false | reasoning_effort="none" |
| 8 | Codex 模型 + thinking=true + reasoning_effort=high | reasoning_effort="high" |
| 9 | 元数据字段（use/name/supports_thinking） | 不出现在构造参数中 |
| 10 | is_tracing_enabled=true 但 LangSmith 导入失败 | warning，不阻断 |
| 11 | 反射路径拼写错误 | ImportError 含安装提示 |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **配置系统** | `get_app_config()` / `config.get_model_config(name)` |
| **反射系统** | `resolve_class(class_path, base_class)` |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `resolvers.py` | 反射：字符串路径 → 运行时类 | `resolve_class` 的冒号分割 + `issubclass` 校验 + 缺失依赖的可操作提示 |
| `credential_loader.py` | 凭证链式加载 | Claude 的 4 级降级来源；Codex 的 legacy/当前结构兼容；文件描述符读取 |
| `factory.py` | 工厂入口 | 序列化 exclude 集合；thinking 快捷方式深度合并算法；Codex 特殊映射；kwargs 在前 config 在后 |
| `claude_provider.py` | Claude 增强提供商 | `model_post_init` 凭证检测 + OAuth Bearer 切换；`_get_request_payload` 缓存/预算注入；速率限制重试 |
| `openai_codex_provider.py` | Codex Responses API | 完整自实现 `BaseChatModel`（非继承）；SSE 流式解析；`_convert_messages` LangChain→Responses 格式 |
| `patched_openai.py` | Gemini thought_signature | `_get_request_payload` 拦截载荷 + `additional_kwargs` 回填非标准字段 |
| `patched_deepseek.py` | DeepSeek reasoning_content | 同上模式，回填 `reasoning_content` |
| `patched_minimax.py` | MiniMax reasoning_split | 注入 `extra_body.reasoning_split=true`；`_convert_chunk_to_generation_chunk` 中提取 `reasoning_details` |

源码文件见同目录下的 `src/` 子文件夹。
