# 模型配置 — LLM 模型声明

## 模块路径

`deerflow.config.model_config`

## ModelConfig 字段详解

### 基础字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 模型唯一标识符，Agent 运行时通过此名称选择模型 |
| `display_name` | `str \| None` | UI 显示名称 |
| `description` | `str \| None` | 模型描述 |
| `use` | `str` | langchain 模型类的完整路径（如 `langchain_openai.ChatOpenAI`） |
| `model` | `str` | 传递给 Provider 的模型名称（如 `gpt-4o`、`claude-3-opus`） |

### 能力标记

| 字段 | 类型 | 说明 |
|------|------|------|
| `supports_thinking` | `bool` | 模型是否支持扩展思考（reasoning） |
| `supports_reasoning_effort` | `bool` | 模型是否支持推理力度调节 |
| `supports_vision` | `bool` | 模型是否支持图像输入 |

### Thinking 切换

| 字段 | 类型 | 说明 |
|------|------|------|
| `thinking` | `dict \| None` | thinking 参数的简写形式 |
| `when_thinking_enabled` | `dict \| None` | thinking 开启时追加的参数 |
| `when_thinking_disabled` | `dict \| None` | thinking 关闭时追加的参数 |

`thinking` 字段是 `when_thinking_enabled` 的语法糖。如果两者都提供，会合并。

#### vLLM 思考核心配置

```yaml
models:
  - name: qwen-reasoning
    use: deerflow.models.vllm_provider:VllmChatModel
    model: Qwen/Qwen3-32B
    supports_thinking: true
    when_thinking_enabled:
      extra_body:
        chat_template_kwargs:
          enable_thinking: true
    when_thinking_disabled:
      extra_body:
        chat_template_kwargs:
          enable_thinking: false
```

### OpenAI 特定

| 字段 | 类型 | 说明 |
|------|------|------|
| `use_responses_api` | `bool \| None` | 是否使用 /v1/responses API |
| `output_version` | `str \| None` | 结构化输出版本 |

### extra="allow"

模型配置允许任意额外字段直接透传到模型构造函数。
不同 Provider 的特定参数不需要在配置系统中定义：

```yaml
models:
  - name: my-model
    use: langchain_openai.ChatOpenAI
    model: gpt-4o
    temperature: 0.7        # 直接透传
    max_tokens: 4096        # 直接透传
```

## 与模型工厂的关系

配置只声明"有什么模型"和"模型有什么能力"。
创建模型实例由 `deerflow.models.factory.create_chat_model()` 负责：

```
ModelConfig（声明）
    ↓
create_chat_model(name, thinking_enabled)
    ├── 通过 name 查找 ModelConfig
    ├── resolve_variable(use) → 导入模型类
    ├── 合并 thinking 参数
    └── 构造模型实例
```
