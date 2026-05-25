# 追踪系统 — 全局概览

## 定位

DeerFlow 追踪模块（`deerflow.tracing`）负责将 Agent 运行过程中的 LLM 调用、工具执行、中间状态等信息发送到外部可观测性平台。它为 LangChain 生态中的主流追踪服务（LangSmith、Langfuse）提供统一的初始化入口和回调构建器，使 Agent 运行可观测、可调试、可分析。

> **关键边界**：追踪模块只负责"构建追踪回调并注入运行时"，不负责"决定追踪哪些事件"。事件的选择和捕获由 LangChain Callback 机制和各追踪平台的 CallbackHandler 实现。

## 源文件

```
backend/packages/harness/deerflow/tracing/
└── factory.py    # build_tracing_callbacks() 及 Provider 工厂函数
```

## 解决的核心问题

| 问题 | 追踪模块的解决方案 |
|------|---------------------|
| **多平台适配** | 支持 LangSmith 和 Langfuse 两个主流追踪平台，通过统一接口 `build_tracing_callbacks()` 返回回调列表 |
| **配置驱动启用** | 从 `get_enabled_tracing_providers()` 读取启用的追踪平台，未配置则返回空列表（零开销） |
| **延迟导入** | 追踪 SDK 只在需要时导入（`from langchain_core.tracers.langchain import LangChainTracer`），未启用时不增加启动依赖 |
| **错误隔离** | 单个 Provider 初始化失败抛出 `RuntimeError` 并携带平台名称，方便定位问题 |

## 核心函数详解

### `build_tracing_callbacks()`

构建所有已启用追踪平台的回调处理器列表。

**流程**：

```python
def build_tracing_callbacks() -> list[Any]:
    # 1. 验证配置的 Provider 名称是否合法
    validate_enabled_tracing_providers()

    # 2. 获取已启用的 Provider 列表
    enabled_providers = get_enabled_tracing_providers()
    if not enabled_providers:
        return []

    # 3. 加载追踪配置（密钥、项目名等）
    tracing_config = get_tracing_config()

    # 4. 逐个创建 Provider 的回调处理器
    callbacks = []
    for provider in enabled_providers:
        if provider == "langsmith":
            callbacks.append(_create_langsmith_tracer(tracing_config.langsmith))
        elif provider == "langfuse":
            callbacks.append(_create_langfuse_handler(tracing_config.langfuse))

    return callbacks
```

**返回值**：

- 空列表 `[]`：未启用任何追踪平台
- 包含一个或多个回调处理器的列表：可直接传给 LangChain Agent 的 `config["callbacks"]`

## LangSmith 集成

### 配置

```yaml
# config.yaml 中相关配置
tracing:
  enabled_providers: ["langsmith"]
  langsmith:
    project: "deer-flow-dev"       # LangSmith 项目名称
```

### 实现

```python
def _create_langsmith_tracer(config) -> Any:
    from langchain_core.tracers.langchain import LangChainTracer
    return LangChainTracer(project_name=config.project)
```

- 使用 `LangChainTracer`，它是 LangChain 内置的 LangSmith 追踪回调
- `project_name` 用于在 LangSmith UI 中分组和组织追踪数据
- LangSmith API 密钥通过环境变量 `LANGSMITH_API_KEY` 自动读取（LangChain 内部处理）

### 使用前提

1. 安装依赖：`uv add langchain-core`（通常已作为核心依赖安装）
2. 设置环境变量：`LANGSMITH_API_KEY`
3. 在 `config.yaml` 中启用：`tracing.enabled_providers: ["langsmith"]`

## Langfuse 集成

### 配置

```yaml
# config.yaml 中相关配置
tracing:
  enabled_providers: ["langfuse"]
  langfuse:
    secret_key: "$LANGFUSE_SECRET_KEY"    # 从环境变量读取
    public_key: "$LANGFUSE_PUBLIC_KEY"     # 从环境变量读取
    host: "https://cloud.langfuse.com"     # Langfuse 实例地址（可自托管）
```

### 实现

```python
def _create_langfuse_handler(config) -> Any:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    # Langfuse >= 4 通过客户端单例初始化项目级凭证
    Langfuse(
        secret_key=config.secret_key,
        public_key=config.public_key,
        host=config.host,
    )
    return LangfuseCallbackHandler(public_key=config.public_key)
```

- 先初始化 `Langfuse` 客户端单例（`langfuse >= 4` 的模式），配置密钥和端点
- 再创建 `LangfuseCallbackHandler`，它会自动连接到已初始化的客户端
- 支持自托管 Langfuse 实例（通过 `host` 参数）

### 使用前提

1. 安装依赖：`uv add langfuse`
2. 设置环境变量：`LANGFUSE_SECRET_KEY`、`LANGFUSE_PUBLIC_KEY`
3. 在 `config.yaml` 中启用：`tracing.enabled_providers: ["langfuse"]`

## 配置来源

追踪模块的配置通过 DeerFlow 配置系统获取：

| 函数 | 来源 | 用途 |
|------|------|------|
| `get_enabled_tracing_providers()` | `config.yaml` → `tracing.enabled_providers` | 返回已启用的 Provider 列表（如 `["langsmith"]`） |
| `get_tracing_config()` | `config.yaml` → `tracing` 节 | 返回完整追踪配置对象 |
| `validate_enabled_tracing_providers()` | 内部校验 | 验证 Provider 名称是否合法（`"langsmith"` / `"langfuse"`） |

**配置值中的 `$` 前缀**：配置系统会将 `$VAR_NAME` 解析为环境变量值（如 `$LANGFUSE_SECRET_KEY` → 实际密钥字符串），敏感信息无需硬编码在配置文件中。

## 同时启用多个平台

`build_tracing_callbacks()` 支持同时启用多个追踪平台：

```yaml
tracing:
  enabled_providers: ["langsmith", "langfuse"]
  langsmith:
    project: "deer-flow-prod"
  langfuse:
    secret_key: "$LANGFUSE_SECRET_KEY"
    public_key: "$LANGFUSE_PUBLIC_KEY"
    host: "https://cloud.langfuse.com"
```

此配置下，Agent 的每次运行会同时向 LangSmith 和 Langfuse 发送追踪数据。回调按 Provider 在列表中的顺序依次创建。

## 生命周期

```
应用启动 / Agent 创建
    │
    ▼
build_tracing_callbacks() 被调用
    │
    ▼
validate_enabled_tracing_providers() — 校验 Provider 名称
    │
    ├── 无效名称 → 配置错误，启动失败
    │
    ▼
get_enabled_tracing_providers() — 读取已启用列表
    │
    ├── 空列表 → 返回 []（无追踪开销）
    │
    ▼
get_tracing_config() — 加载密钥和项目配置
    │
    ▼
遍历 enabled_providers，逐个创建回调
    │
    ├── "langsmith" → _create_langsmith_tracer()
    │   └── 延迟导入 LangChainTracer → 配置 project_name → 返回回调
    │
    ├── "langfuse" → _create_langfuse_handler()
    │   └── 延迟导入 Langfuse → 初始化客户端 → 创建 CallbackHandler → 返回回调
    │
    └── 初始化失败 → RuntimeError（含平台名称和原始异常）
    │
    ▼
返回回调列表 → 注入 Agent config["callbacks"]
    │
    ▼
Agent 运行时：LLM 调用、工具执行等事件自动发送到追踪平台
```

## 回调注入方式

追踪回调在 Agent 运行时通过 LangChain 的 `RunnableConfig` 注入：

```python
# 创建 Agent 时获取回调
callbacks = build_tracing_callbacks()

# 运行时注入
config = RunnableConfig(callbacks=callbacks)
result = await agent.ainvoke({"messages": messages}, config=config)
```

LangChain 的回调机制会自动捕获以下事件并发送到追踪平台：

- **LLM 调用**：输入 prompt、模型响应、token 用量、延迟
- **工具调用**：工具名称、输入参数、执行结果、耗时
- **链式执行**：Agent 的完整推理流程，包含决策路径
- **错误信息**：失败的工具调用、模型异常等

## 设计决策

- **延迟导入**：追踪 SDK（`langsmith`、`langfuse`）只在 `build_tracing_callbacks()` 内部按需导入，未启用时不增加启动时间和内存开销
- **统一错误包装**：所有 Provider 初始化异常统一包装为 `RuntimeError`，携带平台名称前缀（如 `"LangSmith tracing initialization failed: ..."`），方便日志定位
- **零开销禁用**：`enabled_providers` 为空时返回空列表，Agent 运行时无额外回调开销
- **配置校验前置**：`validate_enabled_tracing_providers()` 在创建任何回调前执行，避免因拼写错误（如 `"langsmih"`）导致的静默失败
