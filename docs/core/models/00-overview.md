# 模型系统全局概览

## 系统定位

模型系统（`models/`）是 DeerFlow 中连接 **配置层** 和 **Agent 运行时** 的桥梁。它负责将 `config.yaml` 中的声明式模型配置转化为可调用的 `BaseChatModel` 实例，供 Lead Agent 和 Subagent 在运行时使用。

```
config.yaml → AppConfig → ModelConfig → create_chat_model() → BaseChatModel 实例
                                             ↑
                                       反射系统 (resolve_class)
                                             ↓
                                       Provider 类 (ClaudeChatModel, VllmChatModel, ...)
```

在整个 DeerFlow 架构中，模型系统的位置如下：

| 层次 | 模块 | 职责 |
|------|------|------|
| 配置层 | `config/model_config.py` | 声明有哪些模型、模型有什么能力 |
| **模型工厂** | **`models/factory.py`** | **将声明转化为实例，处理 thinking/vision 切换** |
| Provider 层 | `models/claude_provider.py` 等 | 各 LLM Provider 的具体适配实现 |
| 运行时 | `agents/lead_agent/agent.py` | 调用 `create_chat_model()` 获取模型实例 |

## 解决的核心问题

DeerFlow 需要同时支持多种 LLM Provider（Claude、OpenAI、vLLM、DeepSeek、MiniMax、MindIE、Codex），每种 Provider 有不同的：

- **认证方式**：API Key、OAuth Bearer Token、凭据文件
- **API 格式**：Chat Completions、Responses API
- **Thinking 支持**：Anthropic 原生 thinking、OpenAI reasoning_effort、vLLM chat_template_kwargs
- **流式处理**：标准 SSE、XML 工具调用解析、reasoning 字段保留
- **错误恢复**：不同 Provider 的重试策略

模型系统通过 **反射工厂 + Provider 子类化** 统一了这些差异，使上层 Agent 代码无需关心底层 Provider 的具体实现。

## 模块结构

`backend/packages/harness/deerflow/models/` 目录包含 10 个文件：

```
models/
├── __init__.py                  # 模块入口，导出 create_chat_model
├── factory.py                   # 模型工厂，核心创建逻辑
├── claude_provider.py           # Claude Provider：OAuth、Prompt Caching、Thinking Budget
├── credential_loader.py         # 凭据加载器：Claude Code OAuth + Codex CLI
├── vllm_provider.py             # vLLM Provider：reasoning 字段保留
├── openai_codex_provider.py     # Codex Provider：Responses API、SSE 流式
├── patched_deepseek.py          # DeepSeek 补丁：reasoning_content 保留
├── patched_minimax.py           # MiniMax 补丁：reasoning_details 解析
├── patched_openai.py            # OpenAI 补丁：thought_signature 保留（Gemini 兼容）
└── mindie_provider.py           # MindIE Provider：华为昇腾 NPU 兼容、XML 工具调用
```

各文件职责一览：

| 文件 | 类 | 父类 | 核心职责 |
|------|-----|------|----------|
| `factory.py` | — | — | 反射创建模型、thinking 切换、配置合并 |
| `claude_provider.py` | `ClaudeChatModel` | `ChatAnthropic` | OAuth Bearer 认证、Prompt Caching、自动 Thinking Budget |
| `credential_loader.py` | — | — | 从环境变量/文件描述符/凭据文件加载 Claude Code 和 Codex 凭据 |
| `vllm_provider.py` | `VllmChatModel` | `ChatOpenAI` | 保留 vLLM 非标准 `reasoning` 字段 |
| `openai_codex_provider.py` | `CodexChatModel` | `BaseChatModel` | Codex Responses API 完整实现 |
| `patched_deepseek.py` | `PatchedChatDeepSeek` | `ChatDeepSeek` | 多轮对话中保留 `reasoning_content` |
| `patched_minimax.py` | `PatchedChatMiniMax` | `ChatOpenAI` | 解析 `reasoning_details`、剥离 `<think` 标签 |
| `patched_openai.py` | `PatchedChatOpenAI` | `ChatOpenAI` | 保留 `thought_signature`（Gemini via OpenAI gateway） |
| `mindie_provider.py` | `MindIEChatModel` | `ChatOpenAI` | XML 工具调用解析、转义修复、流式回退 |

## 配置体系

### ModelConfig 到 Provider 类的映射

在 `config.yaml` 中，每个模型通过 `use` 字段指定 Provider 类的完整路径：

```yaml
models:
  - name: claude-sonnet-4.6
    use: deerflow.models.claude_provider:ClaudeChatModel
    model: claude-sonnet-4-6
    max_tokens: 16384
    enable_prompt_caching: true

  - name: gpt-4o
    use: langchain_openai:ChatOpenAI
    model: gpt-4o
    api_key: $OPENAI_API_KEY

  - name: qwen-r1
    use: deerflow.models.vllm_provider:VllmChatModel
    model: Qwen/Qwen3-235B-A22B
    base_url: http://localhost:8000/v1
```

`use` 字段的格式为 `模块路径:类名`，通过反射系统 `resolve_class()` 动态导入并实例化。

### 环境变量解析

配置值以 `$` 开头的字符串会被自动解析为环境变量：

```yaml
api_key: $OPENAI_API_KEY       # → os.getenv("OPENAI_API_KEY")
base_url: $VLLM_BASE_URL       # → os.getenv("VLLM_BASE_URL")
```

解析由 `AppConfig.resolve_env_variables()` 在配置加载时统一处理，若环境变量不存在则抛出 `ValueError`。

### 能力标记

`ModelConfig` 定义了模型的能力标记，影响运行时行为：

| 字段 | 类型 | 作用 |
|------|------|------|
| `supports_thinking` | `bool` | 是否支持扩展思考模式 |
| `supports_vision` | `bool` | 是否支持图像输入 |
| `supports_reasoning_effort` | `bool` | 是否支持 reasoning_effort 参数 |
| `when_thinking_enabled` | `dict` | thinking 开启时追加的参数 |
| `when_thinking_disabled` | `dict` | thinking 关闭时追加的参数 |
| `thinking` | `dict` | thinking 参数的简写形式 |

## 关键设计决策

### 1. 反射机制

模型工厂使用 `resolve_class(model_config.use, BaseChatModel)` 动态导入 Provider 类，而非硬编码 if-else 分支。这意味着：

- 新增 Provider 只需编写子类并在 `config.yaml` 中配置 `use` 路径
- 无需修改 `factory.py` 本身
- 第三方 Provider 可以通过自定义类路径集成

### 2. Thinking Budget 自动分配

`ClaudeChatModel` 在 `_apply_thinking_budget()` 中自动将 `max_tokens` 的 80% 分配给 thinking budget（`THINKING_BUDGET_RATIO = 0.8`），用户无需手动计算。

### 3. OAuth 凭据加载链

`credential_loader.py` 实现了一个多层级的凭据查找策略，从显式环境变量到文件描述符再到凭据文件，确保在不同部署环境下都能获取到凭据：

```
环境变量 → 文件描述符 → 凭据文件路径 → 默认路径 (~/.claude/.credentials.json)
```

### 4. Provider 子类化而非 Wrapper

所有 Provider 适配都通过继承 LangChain 的 `BaseChatModel` 子类实现，覆写 `_get_request_payload()`、`_generate()`、`_create_chat_result()` 等方法。这种设计的优势：

- 完全兼容 LangChain 生态（回调、流式、工具绑定）
- 最小化侵入性，只需覆写必要的钩子
- 保持与上游 LangChain 版本的兼容性

### 5. 流式处理标准化

不同 Provider 的流式响应差异在各自 Provider 类中统一处理：

- vLLM 的 `reasoning` 字段 → 映射到 `additional_kwargs.reasoning_content`
- MiniMax 的 `reasoning_details` → 提取文本到 `additional_kwargs.reasoning_content`
- MindIE 的 XML 工具调用 → 解析为标准 `tool_calls`
- Codex 的 SSE 事件 → 聚合为 `ChatResult`

所有 Provider 最终都输出 LangChain 标准的消息格式，上层代码无需感知差异。
