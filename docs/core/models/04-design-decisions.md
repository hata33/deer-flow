# 04 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **反射工厂 resolve_class 动态加载** | 新增 Provider 只需写子类 + 改配置，不碰工厂代码 |
| 2 | **thinking_enabled 标志 + per-model 覆盖** | 统一 4 种 Provider 的 thinking 差异，运行时切换无需重启 |
| 3 | **VllmChatModel 继承 ChatOpenAI** | 保留 vLLM 非标准 reasoning 字段，而非丢弃导致多轮中断 |
| 4 | **$VAR 环境变量解析** | 密钥不落盘，配置文件可安全提交 Git |
| 5 | **supports_vision 控制工具注入** | 不支持图像的模型收到 base64 图片会报错，必须前置过滤 |
| 6 | **use_responses_api 独立开关** | OpenAI /v1/responses 端点语义不同，需显式启用而非自动推断 |

---

## 二、逐决策分析

### 决策 1：反射工厂 resolve_class 动态加载

**问题**：DeerFlow 支持 8+ 种 LLM Provider（Claude、OpenAI、vLLM、DeepSeek、MiniMax、MindIE、Codex、Gemini），如何在不修改工厂代码的前提下扩展？

| 方案 | 优势 | 劣势 |
|------|------|------|
| if-else 分支（硬编码） | 简单直接 | 每加一个 Provider 改一次 factory.py |
| 插件注册表（装饰器） | 显式声明 | Provider 必须被 import 才能注册 |
| **反射加载（当前）** | 配置驱动，零代码改动 | 字符串拼错运行时才报错 |

**选择反射加载**：`config.yaml` 中的 `use: deerflow.models.vllm_provider:VllmChatModel` 被 `resolve_class()` 动态解析为 Python 类。新增 Provider 的步骤：

1. 编写 `BaseChatModel` 子类
2. 在 `config.yaml` 中添加 `use` 路径
3. 不修改 `factory.py`

`resolve_class()` 还通过 `issubclass(model_class, BaseChatModel)` 进行类型校验，防止配置错误导致的运行时崩溃。

---

### 决策 2：thinking_enabled 标志 + per-model 覆盖

**问题**：4 种 Provider 的 thinking 实现完全不同：

| Provider | thinking 控制方式 | 字段位置 |
|----------|------------------|----------|
| Anthropic | `thinking.type = enabled/disabled` | 构造函数参数 |
| OpenAI 网关 | `extra_body.thinking.type` | 嵌套在 extra_body |
| vLLM/Qwen | `chat_template_kwargs.enable_thinking` | 嵌套在 extra_body |
| Codex | `reasoning_effort = none/low/medium/high` | 独立参数 |

| 方案 | 优势 | 劣势 |
|------|------|------|
| 每个 Provider 独立处理 thinking | 最大灵活性 | Agent 代码散布 Provider 细节 |
| **统一标志 + 条件配置合并（当前）** | Agent 只传 `thinking_enabled=True` | thinking 切换逻辑集中在 factory |

**选择统一标志**：Agent 调用 `create_chat_model("qwen-r1", thinking_enabled=True)`，factory 通过 `when_thinking_enabled` 配置自动注入 Provider 特定参数。禁用时通过 4 条分支分别处理 Anthropic 原生、OpenAI 网关、vLLM、Codex 的差异。

`thinking` 快捷字段与 `when_thinking_enabled` 递归合并，允许用户用简写配置：
```yaml
thinking:                              # 简写
  type: enabled
when_thinking_enabled:                 # 完整配置
  extra_body:
    chat_template_kwargs:
      enable_thinking: true
```

---

### 决策 3：VllmChatModel 继承 ChatOpenAI

**问题**：vLLM 0.19.0 通过 OpenAI 兼容 API 暴露推理模型，在 assistant 消息中返回非标准 `reasoning` 字段。标准 `ChatOpenAI` 在序列化时丢弃该字段，导致后续请求缺少 reasoning → vLLM 无法正确处理交错思维/工具调用流程。

| 方案 | 优势 | 劣势 |
|------|------|------|
| Wrapper 模式（组合） | 解耦 | 需要代理所有 ChatOpenAI 方法 |
| **子类化 + 覆写（当前）** | 最小侵入，只改 3 个方法 | 继承 ChatOpenAI 的全部行为 |
| Monkey-patch | 零子类 | 全局影响，不可控 |

**选择子类化**：覆写 `_get_request_payload()`（多轮回传 reasoning）、`_create_chat_result()`（非流式保留）、`_convert_chunk_to_generation_chunk()`（流式保留），只修改必要的行为。

reasoning 字段双重保存到 `additional_kwargs`：
- `reasoning`：原始值（用于回传 vLLM）
- `reasoning_content`：提取的文本（用于前端展示）

---

### 决策 4：$VAR 环境变量解析

**问题**：API Key 等密钥不能明文写在 `config.yaml` 中（会被提交到 Git）。

| 方案 | 优势 | 劣势 |
|------|------|------|
| .env 文件注入 | 自动加载 | 不明确哪些值需要环境变量 |
| **$ 前缀标记 + 递归解析（当前）** | 显式声明，配置文件可提交 | 变量不存在时运行时报错 |
| 运行时全部读 env | 不需要配置文件 | 失去 YAML 声明式优势 |

**选择 $ 前缀**：`AppConfig.resolve_env_variables()` 递归遍历所有配置值，`$OPENAI_API_KEY` → `os.getenv("OPENAI_API_KEY")`。变量不存在时抛出 `ValueError`——配置错误应尽早发现，不应静默失败。

配置文件中密钥位置一目了然：
```yaml
api_key: $OPENAI_API_KEY      # 明确标记需要环境变量
base_url: $VLLM_BASE_URL      # 同上
```

---

### 决策 5：supports_vision 控制工具注入

**问题**：`view_image` 工具将图片转为 base64 注入 LLM 上下文。不支持图像的模型（如纯文本 GPT-3.5）收到 base64 数据会返回错误或静默忽略。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 工具注册后由 LLM 自行决定是否调用 | 简单 | 浪费 context 空间，可能误调用 |
| **前端 + 后端双重过滤（当前）** | 精确控制 | 需要维护能力标记 |

**选择能力标记过滤**：`ModelConfig.supports_vision` 在配置中声明，`get_available_tools()` 据此决定是否包含 `view_image`。`ViewImageMiddleware` 也在运行时检查 `supports_vision`，不注入图片数据。双保险避免非视觉模型收到图像数据。

---

### 决策 6：use_responses_api 独立开关

**问题**：OpenAI 的 `/v1/responses` 端点（Codex）与 Chat Completions API 的消息格式不同——系统提示用 `instructions` 字段，对话用 `input` 数组，工具调用用 `function_call` 类型。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 通过 `use` 字段自动推断 | 减少配置 | 绑定 Provider 类到 API 端点 |
| **独立 `use_responses_api` 标志（当前）** | 解耦 Provider 和 API 版本 | 多一个配置字段 |

**选择独立标志**：`use_responses_api: true` 允许使用 `langchain_openai:ChatOpenAI`（标准类）走 Responses API 端点，而不必创建专门的 Provider 子类。`output_version` 字段控制结构化输出的版本格式。这使得同一个 Provider 类可以根据配置切换 API 端点。

---

## 三、设计效果

| 效果 | 实现方式 |
|------|----------|
| **零代码扩展** | 新 Provider 只需子类 + 配置，不动 factory |
| **统一 thinking 切换** | Agent 传一个布尔值，factory 处理 4 种差异 |
| **密钥安全** | `$VAR` 模式，配置文件可提交 Git |
| **Provider 兼容** | 子类化覆写最小化侵入，保持 LangChain 生态兼容 |
| **视觉安全** | `supports_vision` 双重过滤，非视觉模型不收图片 |
| **API 灵活** | `use_responses_api` 解耦 Provider 类与 API 端点 |
