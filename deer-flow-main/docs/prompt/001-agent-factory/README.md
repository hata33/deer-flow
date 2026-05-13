# 001-Agent工厂构建模块

> 已验证来源：deer-flow 项目 `agents/lead_agent/agent.py` + `agents/factory.py` + `agents/features.py`
> 本提示词可在新项目中直接使用，通过适配层注入新项目的框架差异，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

在 AI Agent 系统中，每次用户请求到达时需要动态组装一个完整的 Agent 实例。这个实例由多个部件组成（模型、工具、中间件、系统提示词、状态模式），且每个部件的创建策略不同——有的依赖请求参数，有的依赖配置文件，有的需要条件判断后降级。

真实实现是**两层工厂**，不是一层：

```
应用层工厂 make_lead_agent    → 读配置，处理业务策略（模型降级、追踪注入）
SDK 层工厂 create_deerflow_agent → 纯参数组装，不读配置，不依赖全局单例
```

分两层的原因：SDK 层是稳定的公共 API，子智能体/外部集成直接调用；应用层处理业务策略，可以频繁变化。

**解决的核心痛点：**
- 模型提供商差异大（Claude/OpenAI/Codex/Gemini），创建逻辑各不相同
- 同一次请求中需要协调多个部件（模型、工具集、中间件链、提示词模板）
- 运行时参数需要与配置文件合并，且有优先级规则
- 某些能力需要运行时验证（如模型是否支持 thinking），不支持则降级
- 中间件链组装复杂：三态特性标志（True/False/自定义实例）、声明式定位（@Next/@Prev）、终端中间件不变量保护
- 用户工具与特性注入工具需要按 name 去重

---

## 二、输入契约

工厂函数要求的全部外部供给：

| 输入项 | 来源 | 类型 | 说明 |
|--------|------|------|------|
| `config.configurable` | 请求上下文 | dict | 包含 9 个运行时参数，由前端/调用方传入 |
| `config.yaml` 中的 `models` 数组 | 配置文件 | list[ModelConfig] | 声明所有可用模型及其能力标志 |
| `get_app_config()` | 配置加载器 | AppConfig | 应用级配置单例 |
| `resolve_class()` | 反射系统 | callable | 根据类路径字符串动态加载类 |
| `create_agent()` | 框架层 | callable | LangGraph 的 Agent 组装函数 |

### config.configurable 中的 9 个参数

```
model_name          : str | None    -- 请求指定的模型名（最高优先级）
agent_name          : str           -- Agent 标识（决定加载哪套中间件和工具）
thinking_enabled    : bool          -- 是否启用扩展思考
reasoning_effort    : str | None    -- 推理强度（low/medium/high）
groups              : list[str]     -- 工具分组（决定加载哪些工具集）
subagent_enabled    : bool          -- 是否启用子智能体
max_concurrent      : int           -- 最大并发数（影响提示词模板）
plan_mode           : bool          -- 是否为计划模式
stream_modes        : list[str]     -- 流式输出模式（默认 ['values']）
```

### 模型解析三级优先级

```
请求参数 config.configurable["model_name"]     ← 最高
    ↓ (None 时降级)
Agent 配置 agent_config.model                  ← 中等
    ↓ (None 时降级)
全局配置 config.yaml models[0].name             ← 默认兜底
```

---

## 三、输出契约

工厂模块对外承诺提供的一切：

### 对外暴露的接口

```python
def make_lead_agent(config: RunnableConfig) -> CompiledGraph:
    """
    工厂入口函数。每次请求调用一次。

    参数:
        config: LangGraph RunnableConfig，包含 configurable 字典

    返回:
        已编译的 Agent 图（CompiledGraph），可直接调用 astream()

    生命周期:
        每次请求创建新实例，请求结束即丢弃。无状态复用。

    保证:
        - 返回的 Agent 已完全初始化，可直接执行
        - 模型已通过能力验证，不会因不支持特性而在运行时崩溃
        - 中间件链已按正确顺序组装
        - 工具集已根据模型能力和分组过滤
    """
```

### 生成的对象

工厂输出的 Agent 实例由以下部件组装：

```python
create_agent(
    model         = create_chat_model(name, thinking_enabled, reasoning_effort),
    tools         = get_available_tools(model_name, groups, subagent_enabled),
    middleware    = _build_middlewares(config, model_name, agent_name),
    system_prompt = apply_prompt_template(subagent_enabled, max_concurrent, agent_name),
    state_schema  = ThreadState,
)
```

每个子工厂的输出：

| 子工厂 | 输出 | 说明 |
|--------|------|------|
| `create_chat_model()` | `BaseChatModel` 实例 | 已完成认证加载、能力验证、特性配置的模型实例 |
| `get_available_tools()` | `list[BaseTool]` | 根据分组和能力过滤后的工具列表 |
| `_build_middlewares()` | `list[AgentMiddleware]` | 按严格顺序排列的中间件链 |
| `apply_prompt_template()` | `str` | 已填充变量的系统提示词 |

### 副作用与保证

| 保证项 | 说明 |
|--------|------|
| 创建的对象已完全初始化 | 模型认证已加载，中间件已注册，不会在运行时因初始化失败 |
| 能力验证已通过 | `supports_thinking=False` 的模型不会启用 thinking，不会在 API 调用时出错 |
| 降级已自动处理 | thinking 启用但模型不支持时自动降级关闭，不会抛异常给调用方 |
| 无全局状态污染 | 每次调用创建全新实例，不存在跨请求的状态泄漏 |
| 日志已记录 | 模型选择、降级决策、认证来源均有日志输出 |

### 异常契约

| 条件 | 抛出异常 | 调用方处理 |
|------|---------|-----------|
| 指定模型名在配置中不存在 | `ValueError(f"Model {name} not found in config")` | 应返回 400 给用户 |
| 模型不支持 thinking 但强制启用 | `ValueError(f"Model {name} does not support thinking")` | 应返回 400 给用户 |
| 模型类路径无效或加载失败 | `ImportError` / `ModuleNotFoundError` | 应返回 500，检查配置 |
| 凭证缺失（如 Codex 无 auth 文件） | `ValueError` | 应返回 500，检查部署 |
| 配置文件解析失败 | Pydantic `ValidationError` | 应返回 500，启动时即应发现 |

---

## 四、行为约束

### 约束 1：参数优先级必须严格遵循

```
运行时参数 (kwargs) > 配置文件参数 (model_settings_from_config) > 内部默认值
```

实例化时 `model_class(**kwargs, **model_settings_from_config)` 中 kwargs 在前、config 在后，确保运行时覆盖配置。

### 约束 2：能力验证必须在实例化之前

```python
# 正确：先验证后实例化
if thinking_enabled and not model_config.supports_thinking:
    raise ValueError(...)
model_instance = model_class(...)

# 错误：先实例化后验证（模型已创建，可能携带非法参数）
model_instance = model_class(thinking={"type": "enabled"})
if not model_instance.supports_thinking:  # 太晚了
    ...
```

### 约束 3：thinking 禁用必须是显式的

某些模型（如 Anthropic）省略 `thinking` 参数时不会禁用，需要显式设置 `disabled`：

```python
# 正确：显式禁用
kwargs.update({"thinking": {"type": "disabled"}})

# 错误：省略参数（可能导致 thinking 仍然启用）
# 什么都不传
```

### 约束 4：配置过滤必须排除内部字段

```python
model_config.model_dump(
    exclude_none=True,
    exclude={"use", "name", "display_name", "description",
             "supports_thinking", "supports_reasoning_effort",
             "when_thinking_enabled", "thinking", "supports_vision"},
)
```

这些字段是元数据，不能传递给模型构造函数，否则会导致 `TypeError: unexpected keyword argument`。

### 约束 5：工厂函数必须是无状态的

```python
# 正确：每次调用从 config 提取参数
def make_lead_agent(config):
    params = extract_from_config(config)
    return create_agent(**params)

# 错误：使用模块级缓存
_agent_cache = {}
def make_lead_agent(config):
    if config in _agent_cache:  # 危险：跨请求状态泄漏
        return _agent_cache[config]
```

### 约束 6：thinking 快捷方式必须合并到完整配置

```python
# 配置文件可能提供简写
thinking: {type: enabled, budget_tokens: 10000}

# 需要合并到 when_thinking_enabled 结构
effective_wte = dict(model_config.when_thinking_enabled or {})
if model_config.thinking is not None:
    merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
    effective_wte["thinking"] = merged_thinking
```

快捷方式值覆盖默认值，但保留 when_thinking_enabled 中的其他非 thinking 字段。

---

## 五、结构约定

### 文件组织

```
agents/
  lead_agent/
    agent.py          # make_lead_agent() 工厂入口
  factory.py          # create_agent() 通用组装
  features.py         # RuntimeFeatures 三态特性标志
  thread_state.py     # ThreadState 状态模式定义

models/
  __init__.py         # 导出 create_chat_model
  factory.py          # create_chat_model() 模型工厂
  claude_provider.py  # ClaudeChatModel 自定义提供商
  openai_codex_provider.py  # CodexChatModel
  patched_openai.py   # PatchedChatOpenAI
  credential_loader.py # 凭证加载器

config/
  app_config.py       # get_app_config(), get_model_config()
  model_config.py     # ModelConfig Pydantic 模型
```

### 命名规则

| 模式 | 命名 | 示例 |
|------|------|------|
| 工厂函数 | `make_xxx()` 或 `create_xxx()` | `make_lead_agent`, `create_chat_model` |
| 提供商类 | `{Provider}ChatModel` | `ClaudeChatModel`, `CodexChatModel` |
| 配置模型 | `{Module}Config` | `ModelConfig`, `AppConfig` |
| 状态模式 | `{Scope}State` | `ThreadState` |
| 特性标志 | `supports_{capability}` | `supports_thinking`, `supports_vision` |

### 必须遵循的设计模式

1. **工厂模式**：所有对象创建通过工厂函数，禁止直接实例化模型类
2. **策略模式**：不同提供商的认证/协议逻辑封装在各自的 Provider 类中
3. **模板方法模式**：`model_post_init` 作为 Pydantic 钩子，子类在初始化时自动执行凭证加载
4. **建造者模式**：Agent 由 5 个子工厂分别构建部件后组装

---

## 六、最小化契约示意代码

以下代码仅展示结构和契约，具体实现需适配新项目上下文。

### 6.1 Agent 工厂入口

```python
def make_lead_agent(config: RunnableConfig) -> CompiledGraph:
    """每次请求调用，从 config 提取参数，组装 Agent。"""
    params = _extract_configurable(config)  # 9 个参数

    model_name = _resolve_model_name(params)  # 三级优先级
    thinking_enabled = params.get("thinking_enabled", False)

    return create_agent(
        model=_create_model(model_name, thinking_enabled, params),
        tools=_create_tools(model_name, params),
        middleware=_create_middleware(params),
        system_prompt=_create_prompt(params),
        state_schema=AppState,
    )
```

### 6.2 模型子工厂

```python
def _create_model(name: str | None, thinking_enabled: bool, params: dict) -> BaseChatModel:
    """模型子工厂：配置加载 → 能力验证 → 特性处理 → 实例化 → 追踪附加"""
    config = get_app_config()

    # 默认模型降级
    if name is None:
        name = config.models[0].name

    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config")

    # 反射加载模型类
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 过滤内部字段，只保留提供商参数
    settings = model_config.model_dump(exclude_none=True, exclude=INTERNAL_FIELDS)

    # thinking 特性处理
    if thinking_enabled:
        _validate_thinking_support(model_config, name)  # 不支持则 raise
        settings.update(_merge_thinking_config(model_config))
    else:
        _apply_thinking_disabled(model_config, settings)  # 显式禁用

    # 实例化（运行时参数覆盖配置参数）
    instance = model_class(**params.get("runtime_kwargs", {}), **settings)

    # 可选：追踪附加
    _attach_tracing_if_enabled(instance)

    return instance
```

### 6.3 三态特性标志（可选子模块）

```python
class RuntimeFeatures:
    """每个特性字段接受三种值：
    True  → 使用内置默认中间件
    False → 禁用
    AgentMiddleware 实例 → 自定义替换
    """
    summarization: bool | AgentMiddleware = False
    guardrail: bool | AgentMiddleware = False
    token_usage: bool | AgentMiddleware = False

    # 对于没有合理默认值的特性，True 必须报错
    # summarization=True 会 raise ValueError("需要提供自定义中间件")
```

---

## 七、自由度与禁区

### 可以改的

- 工厂函数名（`make_lead_agent` → `build_agent` / `create_xxx_agent`）
- 文件组织结构（单文件 / 多文件 / 包结构）
- 具体的 Provider 类（新项目可能用不同的模型提供商）
- 配置格式（YAML / TOML / JSON / 环境变量）
- 依赖注入方式（FastAPI Depends / 手动传参 / 全局单例）
- `ThreadState` 的字段定义（根据业务需求增减）
- 中间件的种类和数量

### 不能改的

- **参数优先级链**：运行时 > 配置文件 > 默认值，这个顺序是安全网
- **能力验证先于实例化**：防止不支持的特性在运行时崩溃
- **thinking 禁用必须显式**：省略不等于禁用，某些 SDK 会默认启用
- **工厂函数必须无状态**：每次调用创建新实例，禁止跨请求缓存
- **配置过滤排除内部字段**：元数据字段不能传给构造函数
- **快捷方式合并逻辑**：thinking 快捷方式的合并必须是深度合并，浅合并会丢失嵌套字段

---

## 八、验证场景

以下场景可直接转化为单元测试，跑通才算通过。

### 场景 1：默认模型降级

```
给定: config.yaml 中 models = [{name: "gpt-4"}], 请求不指定 model_name
调用: make_lead_agent(config)
期望: 使用的模型为 "gpt-4"
```

### 场景 2：请求参数优先级最高

```
给定: config.yaml 中 models[0].name = "gpt-4", 请求指定 model_name = "claude"
调用: make_lead_agent(config_with_model_name="claude")
期望: 使用的模型为 "claude"，而非 "gpt-4"
```

### 场景 3：模型不支持 thinking 时拒绝

```
给定: model_config.supports_thinking = False, thinking_enabled = True
调用: _create_model("model-a", thinking_enabled=True, ...)
期望: 抛出 ValueError，包含 "does not support thinking"
```

### 场景 4：thinking 禁用是显式的

```
给定: model_config 有 when_thinking_enabled 配置, thinking_enabled = False
调用: _create_model(...)
期望: 传给模型构造函数的参数中包含 thinking.type = "disabled"
      而不是省略 thinking 参数
```

### 场景 5：配置过滤不泄露内部字段

```
给定: model_config.use = "langchain_openai:ChatOpenAI"
      model_config.name = "gpt-4"
      model_config.supports_thinking = True
调用: _create_model(...)
期望: 传给 ChatOpenAI 构造函数的参数中不包含 "use", "name", "supports_thinking"
```

### 场景 6：工厂无状态

```
给定: 两次调用 make_lead_agent，使用不同的 config
调用:
  agent_a = make_lead_agent(config_a)  # model="gpt-4"
  agent_b = make_lead_agent(config_b)  # model="claude"
期望: agent_a 和 agent_b 使用不同的模型实例，互不影响
```

### 场景 7：模型不存在时报错

```
给定: config.yaml 中没有名为 "nonexistent" 的模型
调用: _create_model("nonexistent", ...)
期望: 抛出 ValueError("Model nonexistent not found in config")
```

### 场景 8：快捷方式合并保留嵌套字段

```
给定:
  model_config.when_thinking_enabled = {"thinking": {"type": "enabled"}, "extra_body": {"x": 1}}
  model_config.thinking = {"budget_tokens": 10000}
调用: _merge_thinking_config(model_config)
期望: 合并结果为 {"thinking": {"type": "enabled", "budget_tokens": 10000}, "extra_body": {"x": 1}}
      extra_body 字段未被丢失
```

---

## 九、依赖的上下游模块

本模块在积木体系中的位置：

```
[上游] 配置系统模块 → 提供 ModelConfig、AppConfig
[上游] 反射系统模块 → 提供 resolve_class()
[上游] 状态模式模块 → 提供 ThreadState 定义
    ↓
[本模块] Agent 工厂构建
    ↓
[下游] 中间件链模块 → 消费工厂输出的 middleware 列表
[下游] Agent Loop 模块 → 消费工厂输出的 Agent 实例
[下游] 工具系统模块 → 消费工厂输出的 tools 列表
```

在新项目中组合时，先确认上游模块的接口契约与本模块的输入契约一致，再组装下游模块。

---

## 十、适配指引

在新项目中使用本模块时，需要提供的适配层信息：

### 必须提供

1. **你的配置格式**：告诉 AI 你的配置文件格式（YAML/TOML/JSON）、如何加载、ModelConfig 的字段
2. **你的模型提供商**：列出你使用的模型（如只用 OpenAI，就不需要 Claude/Codex 的 Provider）
3. **你的 Agent 框架**：如果不是 LangGraph，需要映射 `create_agent` 到你的框架 API
4. **你的状态模式**：ThreadState 中哪些字段你需要保留、哪些不需要

### 可选提供

5. **你的依赖注入方式**：FastAPI / Flask / 手动传参
6. **你需要的中间件列表**：从 17 个中间件中选择需要的
7. **你的工具集**：工具分组策略和具体工具列表
8. **你的认证方式**：是否需要 OAuth，还是只用 API Key

### 示例适配层提示词

```
## 适配层：我的新项目

- 框架：FastAPI + LangGraph
- 配置：使用 pyproject.toml 的 [tool.myagent] 段
- 模型：只用 OpenAI gpt-4o 和 Claude claude-sonnet-4
- 状态：只需要 messages 和 artifacts，不需要 sandbox
- 中间件：需要 summarization、token_usage、loop_detection，不需要 sandbox 相关
- 认证：只用 API Key，不需要 OAuth
- DI 方式：FastAPI app.state 单例

请根据以上适配信息，基于工厂构建模块的契约，生成适配新项目的代码。
```
