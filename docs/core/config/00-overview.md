# 配置系统 — 全局概览

## 系统定位

配置系统是 DeerFlow 的基础设施层，为所有子系统（Agent、沙箱、模型、工具、MCP、记忆等）提供统一的配置管理。它负责从磁盘文件加载配置、校验合法性、解析环境变量、缓存实例、检测变更并自动热更新。

## 模块路径

`deerflow.config`

## 能力来源

### 两个配置文件

| 文件 | 格式 | 用途 | 修改方式 |
|------|------|------|----------|
| `config.yaml` | YAML | 静态配置（模型、工具、沙箱等） | 手动编辑 |
| `extensions_config.json` | JSON | 动态配置（MCP 服务器、技能开关） | API 驱动 |

分离的原因：
- config.yaml 包含模型密钥、工具声明等敏感/静态配置，不适合 API 动态修改
- extensions_config.json 包含运行时可增减的 MCP 服务器和技能状态，需要 API 实时修改

### 文件位置解析

两个配置文件都有相同的多级查找策略：

```
显式参数 → 环境变量 → 项目根目录 → 传统 monorepo 位置 → 错误/空配置
```

### 环境变量解析

配置值以 `$` 开头的自动解析为环境变量（如 `$OPENAI_API_KEY`）。解析时机：
- config.yaml: 由 `AppConfig.resolve_env_variables()` 在加载时处理
- extensions_config.json: 由 `ExtensionsConfig.resolve_env_variables()` 在加载时处理

### 配置缓存

```
get_app_config()                    ← app_config.py
    ├── ContextVar 覆盖（最高优先级）
    ├── 自定义配置（set_app_config 注入，不自动刷新）
    └── 文件缓存（mtime 变更时自动重新加载）

get_extensions_config()             ← extensions_config.py
    └── 内存缓存（手动 reload 触发刷新）
```

### ContextVar 覆盖栈

`push/pop_current_app_config()` 提供协程安全的配置覆盖：
- 测试中注入临时配置
- LangGraph 运行时为不同线程使用不同配置
- 支持嵌套 push/pop（栈式管理）

## 核心设计决策

### 为什么有些配置是全局单例，有些是 AppConfig 字段

全局单例模式（如 `_memory_config`、`_title_config`）：
- 存在历史原因：这些子系统最初独立开发，各自管理自己的全局状态
- 存在便利性原因：很多代码路径只访问一个子配置，不需要拿到完整的 AppConfig

AppConfig 字段模式（如 `database`、`run_events`、`token_usage`）：
- 新增的配置倾向于直接作为 AppConfig 字段
- 调用方通过 `get_app_config().database` 访问

`AppConfig._apply_singleton_configs()` 负责在加载时将 AppConfig 中的子配置分发到各全局单例，保持兼容性。

### 为什么追踪配置用环境变量而非 config.yaml

- 追踪是运维关注点，不应混入应用配置
- LangSmith/Langfuse 的 SDK 本身就读环境变量
- 环境变量在 CI/CD 和容器编排中更易管理

### 为什么每层配置都允许 extra="allow"

不同 Provider（模型、工具、沙箱）有自己的特定参数。
`extra="allow"` 让这些参数直接透传到 Provider 构造函数，
不需要在配置系统中为每个 Provider 定义专用字段。

## 模块结构

```
config/
├── __init__.py              ← 公开接口导出
├── app_config.py            ← 根配置 AppConfig + 缓存 + ContextVar 覆盖栈
├── runtime_paths.py         ← 项目根目录和状态目录定位（最底层依赖）
├── paths.py                 ← 文件系统路径管理（线程目录、虚拟路径、Docker 挂载）
├── extensions_config.py     ← MCP 服务器和技能状态配置（extensions_config.json）
│
├── model_config.py          ← LLM 模型声明与能力标记
├── database_config.py       ← 统一数据库后端（memory/sqlite/postgres）
├── sandbox_config.py        ← 沙箱系统（Provider + Docker 参数 + 输出截断）
├── subagents_config.py      ← 子代理（全局配置 + per-agent 覆盖 + 自定义代理）
│
├── tool_config.py           ← 工具声明与分组
├── tool_search_config.py    ← 工具延迟加载（tool_search）
├── skills_config.py         ← 技能目录定位与容器路径
├── skill_evolution_config.py← Agent 自主技能演化
│
├── memory_config.py         ← 记忆系统（存储、防抖、容量限制）
├── summarization_config.py  ← 对话摘要（触发策略、保留策略、技能保留）
├── title_config.py          ← 自动标题生成
│
├── guardrails_config.py     ← 工具调用前置授权
├── loop_detection_config.py ← 重复工具调用检测
├── token_usage_config.py    ← Token 用量追踪开关
├── circuit_breaker_config.py(在 app_config.py 中) ← LLM 熔断器
│
├── checkpointer_config.py   ← LangGraph Checkpointer 后端
├── stream_bridge_config.py  ← Agent → SSE 桥接后端
├── run_events_config.py     ← 运行事件存储后端
│
├── tracing_config.py        ← LangSmith/Langfuse 追踪（环境变量驱动）
├── agents_config.py         ← 自定义代理配置加载
├── agents_api_config.py     ← 自定义代理管理 API 开关
└── acp_config.py            ← ACP 兼容代理配置
```

## 与其他模块的关系

```
runtime_paths.py ←── 所有需要路径的模块
       ↓
paths.py ←── sandbox/、agents/、memory/、skills/
       ↓
app_config.py ←── agents/（make_lead_agent）、runtime/（checkpointer、store）
       ↓                    gateway/（app.py 启动时加载）
extensions_config.py ←── mcp/（工具加载）、skills/（技能发现）
```
