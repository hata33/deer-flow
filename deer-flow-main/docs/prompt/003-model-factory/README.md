# 003-模型工厂模块

> 已验证来源：deer-flow 项目 `models/factory.py` + `models/claude_provider.py` + `models/openai_codex_provider.py` + `models/credential_loader.py` + `reflection/resolvers.py` + 三个 patched 提供商
> 本提示词可在新项目中直接使用，通过适配层注入新项目的模型提供商列表和认证方式，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

AI Agent 系统需要同时支持多家模型提供商，每家的认证方式（API Key / OAuth / CLI token）、API 协议（Chat Completions / Responses API / 原生 SDK）、thinking 处理（直接参数 / extra_body 嵌套）、流式格式（SSE / WebSocket）各不相同。
上层 Agent 工厂不应关心这些差异——它只传模型名 + 能力参数，拿到统一的 `BaseChatModel` 实例即可。

**解决的核心痛点：**
- 新增模型提供商需改工厂代码 → 反射加载，只改配置
- 元数据字段泄漏到构造函数 → 序列化 exclude 集合
- thinking 禁用不彻底 → 显式 disabled 注入，区分两种嵌套位置
- reasoning_effort 全局配置导致部分模型报错 → 不支持时静默移除
- SDK bug 导致多轮对话丢失字段 → Patched 子类在载荷层回填
- 凭证来源多样 → 链式降级自动发现

---

## 二、输入契约

| 输入项 | 来源 | 说明 |
|--------|------|------|
| `name` | 调用参数 | 模型名称，None 时降级到配置第一个 |
| `thinking_enabled` | 调用参数 | 是否启用思考模式 |
| `**kwargs` | 调用参数 | 额外参数（如 reasoning_effort） |
| `ModelConfig` | 配置系统 | 模型的完整配置定义 |
| `resolve_class()` | 反射系统 | 字符串路径 → 运行时类 |

### 工厂处理流水线

```
配置查找(name → ModelConfig)
    → 反射加载(use → model_class)
    → 序列化配置(exclude 元数据字段)
    → 合并 thinking 快捷方式
    → 适配 thinking 启用/禁用
    → 移除不支持的能力参数
    → Codex 特殊映射
    → 实例化(kwargs 在前 config 在后)
    → 追踪附加(可选)
```

---

## 三、输出契约

### 对外暴露的接口

```python
def create_chat_model(name: str | None = None, thinking_enabled: bool = False, **kwargs) -> BaseChatModel:
    """返回已配置完成的模型实例。

    保证：
    - 能力参数已适配（不支持的不传）
    - 凭证已自动加载
    - thinking 状态已显式设置
    - 追踪已按需附加
    """
```

### 保证

| 保证项 | 说明 |
|--------|------|
| 元数据字段已过滤 | `use`/`name`/`supports_*` 不出现在构造参数中 |
| thinking 状态已显式 | 启用时合并配置，禁用时注入 disabled，不会模糊 |
| 能力参数已适配 | 不支持的 reasoning_effort 已移除，不会在 API 调用时出错 |
| 凭证已自动加载 | Provider 的 `model_post_init` 自动发现凭据来源 |
| 追踪不阻断 | LangSmith 注入失败只 warning，不影响模型调用 |

---

## 四、行为约束

### 约束 1：反射加载，禁止硬编码 if-else

```python
# 正确：从配置字符串动态加载
model_class = resolve_class(model_config.use, BaseChatModel)

# 错误：硬编码提供商判断
if provider == "anthropic":
    model_class = ChatAnthropic
elif provider == "openai":
    model_class = ChatOpenAI
```
新增提供商只需改配置，不改工厂。

### 约束 2：序列化排除集合必须完整

```python
exclude = {"use", "name", "display_name", "description",
           "supports_thinking", "supports_reasoning_effort",
           "when_thinking_enabled", "thinking", "supports_vision"}
```
漏掉任何一个，`model_class(**settings)` 都可能报 `TypeError`。

### 约束 3：kwargs 在前 config 在后

```python
model_class(**kwargs, **model_settings_from_config)
```
运行时参数覆盖配置文件参数。顺序反了则覆盖关系反转。

### 约束 4：thinking 快捷方式深度合并

```python
merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
effective_wte = {**effective_wte, "thinking": merged_thinking}
```
保留 `extra_body` 等非 thinking 字段，只覆盖 thinking 子对象。

### 约束 5：Provider 继承而非包装

继承 `ChatAnthropic` / `ChatOpenAI`，在 `model_post_init` 和 `_get_request_payload` 中增强。
包装器无法透传 LangChain 的全部方法（`bind_tools`、`astream`、`ainvoke` 等）。

### 约束 6：Patched 类只修上游 bug

Patched 子类不应添加新功能，只在载荷层回填上游 SDK 静默丢弃的字段。
上游修复后应直接删除 Patched 类，更新 `config.yaml` 中的 `use` 路径。

---

## 五、验证场景

| # | Given | When | Then |
|---|-------|------|------|
| 1 | name=None | create_chat_model() | 使用 config.models[0].name |
| 2 | name 不在配置中 | create_chat_model("x") | ValueError |
| 3 | thinking=true + supports=false | create_chat_model() | ValueError |
| 4 | thinking=false + 有 thinking 配置 | create_chat_model() | 显式注入 disabled |
| 5 | thinking=true + 快捷方式 | create_chat_model() | 深度合并，保留非 thinking 字段 |
| 6 | reasoning_effort + supports=false | create_chat_model() | 静默移除 |
| 7 | Codex + thinking=false | create_chat_model() | reasoning_effort="none" |
| 8 | 元数据字段 | 构造参数 | 不出现 |
| 9 | 追踪启用但导入失败 | create_chat_model() | warning + 正常返回 |
| 10 | 反射路径拼写错误 | resolve_class() | ImportError 含安装命令 |

---

## 六、自由度与禁区

### 可以改的

- 模型提供商列表（按项目需求增减 Provider）
- 凭证来源（只用 API Key，不需要 OAuth/CLI）
- 追踪系统（LangSmith → 其他可观测性工具）
- Patched 类（上游 SDK 修复后删除）
- Codex 特殊映射（不使用 Codex 则无需）

### 不能改的

- **反射加载而非硬编码**：新增提供商只改配置
- **元数据字段排除**：漏掉会 TypeError
- **kwargs 在前 config 在后**：运行时覆盖配置文件
- **thinking 显式禁用**：省略不等于禁用
- **快捷方式深度合并**：浅合并丢失嵌套字段
- **Provider 继承而非包装**：包装器无法透传全部方法

---

## 七、依赖的上下游模块

```
[上游] 配置系统 → get_app_config(), get_model_config()
[上游] 反射系统 → resolve_class(class_path, base_class)
    ↓
[本模块] 模型工厂
    ↓
[下游] Agent 工厂 → create_chat_model(name, thinking_enabled, reasoning_effort)
```
