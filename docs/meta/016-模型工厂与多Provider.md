# 模型工厂与多Provider

**问题**: DeerFlow 需要支持多个 LLM 提供商（OpenAI、Claude、vLLM、MindIE 等），每个有不同的 API 格式、认证方式和特殊能力。直接硬编码会导致代码混乱且无法扩展。

---

## 问题 1：支持哪些 Provider？

| Provider | 特点 | 特殊处理 |
|----------|------|---------|
| OpenAI | 标准 API | 无 |
| Claude | OAuth Bearer + 计费头 | Thinking 参数自动分配预算 |
| vLLM | 自部署推理服务 | `reasoning_effort` 支持 |
| Codex | Responses API | 不同的接口格式 |
| MindIE | 华为昇腾推理 | XML 格式解析 |

每个 Provider 有独立的 `xxx_provider.py` 处理特殊逻辑。

---

## 问题 2：模型创建的完整流程？

八步管线：

```
① 读取 config.yaml 模型配置
    │
    ▼ ② 查找 Provider
根据 model_name 前缀匹配 Provider
    │
    ▼ ③ 反射加载
module_path:class_name 动态导入
    │
    ▼ ④ 序列化配置
将 YAML 配置转为 Provider 参数
    │
    ▼ ⑤ Thinking 参数处理
自动分配 80% max_tokens 给 thinking budget
    │
    ▼ ⑥ Provider 特殊处理
Claude OAuth / vLLM reasoning / Codex Responses API
    │
    ▼ ⑦ 创建实例
BaseChatModel 实例化
    │
    ▼ ⑧ 挂载追踪
LangSmith / Langfuse 追踪装饰
```

---

## 问题 3：配置怎么写？

```yaml
models:
  default: "claude-sonnet-4-20250514"

  providers:
    claude:
      model: "claude-sonnet-4-20250514"
      api_key: "${ANTHROPIC_API_KEY}"
      max_tokens: 8192
      thinking:
        type: "enabled"
        budget_tokens: 6553  # 自动算：8192 * 0.8

    openai:
      model: "gpt-4o"
      api_key: "${OPENAI_API_KEY}"
      max_tokens: 4096

    vllm:
      model: "Qwen/Qwen3-32B"
      base_url: "http://localhost:8000/v1"
      max_tokens: 4096
```

---

## 问题 4：Thinking 参数怎么自动分配？

对于支持 thinking（扩展推理）的模型，系统自动将 `max_tokens` 的 80% 分配给 thinking budget：

```python
thinking_budget = int(max_tokens * 0.8)
# 例：max_tokens=8192 → thinking_budget=6553, 输出 budget=1639
```

为什么 80%：thinking 是内部推理过程，通常比最终输出长得多。80/20 是经验值，在大多数场景下效果良好。

用户可以手动覆盖：

```yaml
thinking:
  type: "enabled"
  budget_tokens: 10000  # 手动指定
```

---

## 问题 5：Claude 的 OAuth 怎么处理？

Claude Provider 需要特殊的认证头：

```python
headers = {
    "Authorization": f"Bearer {api_key}",
    "anthropic-beta": "...",
    "x-api-key": api_key,
}
```

通过 `claude_provider.py` 封装，其他模块不需要关心认证细节。

---

## 问题 6：vLLM 自部署模型怎么接入？

指定 `base_url` 即可：

```yaml
providers:
  my-vllm:
    model: "Qwen/Qwen3-32B"
    base_url: "http://gpu-server:8000/v1"
    api_key: "token-abc"  # 可选
```

系统自动启用 `stream_usage`（自定义 `base_url` 时默认开启），用于 token 统计。

---

## 问题 7：反射加载是什么？

通过配置字符串动态加载 Python 类：

```yaml
providers:
  custom:
    model: "my-model"
    provider_class: "my_company.models:CustomProvider"
```

`reflection` 模块解析 `"my_company.models:CustomProvider"` → 导入模块 → 获取类 → 实例化。

如果模块不存在，会给出安装提示：

```python
MODULE_TO_PACKAGE_HINTS = {
    "langchain_anthropic": "pip install langchain-anthropic",
    "langchain_openai": "pip install langchain-openai",
    ...
}
```

---

## 问题 8：如何为不同任务使用不同模型？

可以为 Agent、子 Agent、压缩摘要等指定不同模型：

```yaml
models:
  default: "claude-sonnet-4-20250514"    # 主 Agent
  subagents:
    default: "gpt-4o-mini"               # 子 Agent 用便宜模型
  summarization:
    model: "gpt-4o-mini"                 # 压缩摘要用便宜模型
```

节省成本——不是所有任务都需要最强的模型。

---

## 数据流概览

```
config.yaml → model configuration
    │
    ▼ factory.py
    │
    ▼ 解析 provider 配置
    │
    ▼ 反射加载 Provider 类
    │
    ▼ Thinking 参数自动分配
    │
    ▼ Provider 特殊处理
    │
    ▼ 创建 BaseChatModel 实例
    │
    ▼ 挂载追踪装饰器
    │
    ▼ 返回可用模型实例
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 模型工厂 | `backend/packages/harness/deerflow/models/factory.py` |
| Claude Provider | `backend/packages/harness/deerflow/models/claude_provider.py` |
| vLLM Provider | `backend/packages/harness/deerflow/models/vllm_provider.py` |
| Codex Provider | `backend/packages/harness/deerflow/models/codex_provider.py` |
| MindIE Provider | `backend/packages/harness/deerflow/models/mindie_provider.py` |
| 反射模块 | `backend/packages/harness/deerflow/reflection/resolvers.py` |

## 深入阅读

- [模型工厂](../core/models/01-factory.md) — 创建流程详解
- [Provider 详解](../core/models/02-providers.md) — 各 Provider 特殊处理
- [反射与配置驱动](021-动态反射与配置驱动.md) — 反射加载原理
