# 06 - StepFun 推理模型适配器：非标准 reasoning 字段的捕获与回放

> 本文档分析 `models/patched_stepfun.py`，解答"国产推理模型（StepFun/DeepSeek）的思维链内容如何在不修改 LangChain 的情况下正确保留"。

---

## 一、问题背景

### 标准 ChatOpenAI 的局限

LangChain 的 `ChatOpenAI` 只处理 OpenAI 规范的标准字段。但国产推理模型返回的思维链内容使用非标准字段名：

| 模型 | 思维链字段名 | 标准？ |
|------|------------|--------|
| OpenAI o1/o3 | `reasoning` | ❌（非标准但广泛使用） |
| DeepSeek R1 | `reasoning_content` | ❌ |
| StepFun | `reasoning` 或 `reasoning_content` | ❌ |

`ChatOpenAI` 的 `_convert_chunk_to_generation_chunk` 和 `_create_chat_result` 会忽略这些字段，导致思维链内容在传输过程中静默丢失。

### 多轮对话的额外问题

即使第一轮捕获了 reasoning，多轮工具调用对话中 LangGraph 会序列化/反序列化消息。`AIMessage.additional_kwargs` 中的 `reasoning_content` 不会自动回放到请求 payload 中，导致模型在后续轮次中丢失之前的思维链上下文。

---

## 二、解决方案

通过继承 `ChatOpenAI` 并覆盖 3 个关键方法，在**不修改 LangChain 源码**的前提下捕获和回放 reasoning 内容：

```
┌──────────────────────────────────────────────────────┐
│ PatchedChatStepFun (extends ChatOpenAI)               │
│                                                      │
│ 请求阶段 (_get_request_payload)                       │
│   └── restore_reasoning_content on historical msgs   │
│                                                      │
│ 流式响应 (_convert_chunk_to_generation_chunk)         │
│   └── 从 delta.reasoning / delta.reasoning_content   │
│       提取 → 存入 additional_kwargs                  │
│                                                      │
│ 非流式响应 (_create_chat_result)                      │
│   └── 从 choice.message.reasoning / reasoning_content│
│       提取 → 存入 additional_kwargs                  │
└──────────────────────────────────────────────────────┘
```

---

## 三、三层拦截详解

### 层 1：请求回放（Request Payload）

```python
def _get_request_payload(self, input_, *, stop=None, **kwargs):
    original_messages = self._convert_input(input_).to_messages()
    payload = super()._get_request_payload(input_, stop=stop, **kwargs)

    restore_assistant_payloads(
        payload.get("messages", []),
        original_messages,
        restore_reasoning_content,
    )
    return payload
```

**为什么需要**：LangGraph 的 checkpoint 恢复历史消息时，`reasoning_content` 在 `AIMessage.additional_kwargs` 中，但 LangChain 构造请求 payload 时不会把它放回 API 请求的 `messages` 数组。`restore_assistant_payloads` 遍历 payload 中的 assistant 消息，把原始消息的 `reasoning_content` 回写进去。

### 层 2：流式响应捕获

```python
def _convert_chunk_to_generation_chunk(self, chunk, ...):
    generation_chunk = super()._convert_chunk_to_generation_chunk(...)

    delta = chunk["choices"][0].get("delta", {})
    reasoning = _extract_reasoning(delta)  # 检查 reasoning 和 reasoning_content
    if reasoning is not _MISSING:
        message = _with_reasoning_content(generation_chunk.message, reasoning)
        # → message.additional_kwargs["reasoning_content"] = reasoning

    return generation_chunk
```

**为什么需要**：StepFun 的流式响应在 `delta` 中携带 reasoning，标准 `ChatOpenAI` 会跳过这个字段。

### 层 3：非流式响应捕获

```python
def _create_chat_result(self, response, ...):
    result = super()._create_chat_result(response, ...)

    for index, generation in enumerate(result.generations):
        choice = choices[index]
        reasoning = _extract_reasoning(choice_message)

        # 兜底：SDK 类型对象可能有 .reasoning 属性
        if reasoning is _MISSING and not isinstance(response, dict):
            reasoning = _extract_reasoning(_get_typed_choice_message(response, index))

        if reasoning is not _MISSING:
            # 替换为携带 reasoning 的消息副本
            patched_generations[index] = ChatGeneration(
                message=_with_reasoning_content(message, reasoning),
            )

    return ChatResult(generations=patched_generations or result.generations, ...)
```

**双重提取策略**：先尝试从 dict 中取，失败则从 SDK 类型对象的属性中取。覆盖不同 SDK 版本的响应格式。

---

## 四、reasoning 提取的多源查找

```python
def _extract_reasoning(value):
    # 1. dict 模式：检查 reasoning_content（DeepSeek 风格）→ reasoning（默认）
    if isinstance(value, Mapping):
        for field in ("reasoning_content", "reasoning"):
            if field in value and value[field] is not None:
                return value[field]

    # 2. 属性模式：getattr 检查
    for field in ("reasoning_content", "reasoning"):
        attr = getattr(value, field, _MISSING)
        if attr is not _MISSING and attr is not None:
            return attr

    # 3. model_extra 模式：某些 SDK 版本把额外字段存在这里
    model_extra = getattr(value, "model_extra", None)
    if isinstance(model_extra, Mapping):
        for field in ("reasoning_content", "reasoning"):
            ...

    return _MISSING
```

三个查找路径覆盖了：
- **原始 dict 响应**（HTTP API 直接返回）
- **SDK 类型对象**（openai SDK 解析后的 Pydantic 对象）
- **model_extra 兜底**（SDK 版本差异导致字段被放入 `model_extra`）

---

## 五、消息不可变性处理

LangChain 的 `AIMessage` 是 Pydantic 模型，修改 `additional_kwargs` 必须通过 `model_copy`：

```python
def _with_reasoning_content(message, reasoning):
    additional_kwargs = dict(message.additional_kwargs)  # 浅拷贝
    additional_kwargs["reasoning_content"] = reasoning
    return message.model_copy(update={"additional_kwargs": additional_kwargs})
```

不直接修改原对象，保持消息的不可变语义。

---

## 六、设计模式

| 模式 | 体现 |
|------|------|
| **子类覆盖** | 继承 `ChatOpenAI`，只覆盖 3 个方法，不改 LangChain |
| **哨兵值** | `_MISSING = object()` 区分 `None`（字段存在但为空）和"字段不存在" |
| **多源降级** | dict → attr → model_extra 三级查找 |
| **请求回放** | `restore_assistant_payloads` 在多轮工具调用中保持 reasoning 上下文 |
| **不可变消息** | `model_copy(update=...)` 而非直接赋值 |

---

## 七、与模型工厂的集成

```python
# models/__init__.py 中的注册
# StepFun 推理模型自动使用 PatchedChatStepFun
# 普通模型继续使用标准 ChatOpenAI
```

适配器通过模型工厂自动选择——用户在 `config.yaml` 中配置 StepFun 模型时，工厂返回 `PatchedChatStepFun` 实例，无需手动指定。

---

## 八、文件索引

| 文件 | 职责 |
|------|------|
| `models/patched_stepfun.py` | StepFun 推理模型适配器 |
| `models/assistant_payload_replay.py` | 多轮消息 reasoning 回放工具 |
| `models/__init__.py` | 模型工厂，根据 provider 选择适配器 |
