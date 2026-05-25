# AppConfig — 根配置详解

## 模块路径

`deerflow.config.app_config`

## 解决的问题

AppConfig 是整个配置系统的根模型，聚合所有子系统的配置。它解决：
1. **单一入口**: 所有配置从一个文件加载，确保一致性
2. **热更新**: mtime 变更时自动检测并重新加载
3. **协程安全**: ContextVar 覆盖栈支持并发使用不同配置
4. **版本管理**: 与 config.example.yaml 比对，提醒用户升级

## 配置字段分类

### 核心运行时

| 字段 | 类型 | 说明 |
|------|------|------|
| `models` | `list[ModelConfig]` | 可用 LLM 模型列表 |
| `tools` | `list[ToolConfig]` | 可用工具列表 |
| `tool_groups` | `list[ToolGroupConfig]` | 工具分组 |
| `sandbox` | `SandboxConfig` | 沙箱系统配置（必需） |
| `database` | `DatabaseConfig` | 数据库后端 |

### Agent 子系统

| 字段 | 类型 | 说明 |
|------|------|------|
| `extensions` | `ExtensionsConfig` | MCP 服务器 + 技能状态 |
| `subagents` | `SubagentsAppConfig` | 子代理配置 |
| `memory` | `MemoryConfig` | 记忆系统 |
| `summarization` | `SummarizationConfig` | 对话摘要 |
| `title` | `TitleConfig` | 自动标题 |
| `guardrails` | `GuardrailsConfig` | 工具调用授权 |
| `loop_detection` | `LoopDetectionConfig` | 循环检测 |
| `circuit_breaker` | `CircuitBreakerConfig` | LLM 熔断器 |

### 技能与工具

| 字段 | 类型 | 说明 |
|------|------|------|
| `skills` | `SkillsConfig` | 技能系统 |
| `skill_evolution` | `SkillEvolutionConfig` | Agent 自主技能演化 |
| `tool_search` | `ToolSearchConfig` | 工具延迟加载 |
| `token_usage` | `TokenUsageConfig` | Token 追踪开关 |

### 基础设施

| 字段 | 类型 | 说明 |
|------|------|------|
| `checkpointer` | `CheckpointerConfig \| None` | Checkpointer 后端 |
| `stream_bridge` | `StreamBridgeConfig \| None` | SSE 桥接后端 |
| `run_events` | `RunEventsConfig` | 运行事件存储 |
| `agents_api` | `AgentsApiConfig` | 自定义代理 API 开关 |
| `acp_agents` | `dict[str, ACPAgentConfig]` | ACP 兼容代理 |

### 全局

| 字段 | 类型 | 说明 |
|------|------|------|
| `log_level` | `str` | deerflow/app 模块日志级别 |

## 加载流程

```
from_file(config_path?)
    │
    ├── 1. resolve_config_path() — 定位 config.yaml
    │   ├── 显式参数
    │   ├── DEER_FLOW_CONFIG_PATH 环境变量
    │   ├── 项目根目录 config.yaml
    │   └── 传统 monorepo 位置
    │
    ├── 2. yaml.safe_load() — 解析 YAML
    │
    ├── 3. _check_config_version() — 版本检查
    │   ├── 读取 config.example.yaml 的 config_version
    │   ├── 与用户 config.yaml 的 config_version 比对
    │   └── 用户版本 < 示例版本 → 警告日志
    │
    ├── 4. resolve_env_variables() — 递归解析 $VAR
    │   ├── "$OPENAI_API_KEY" → os.getenv("OPENAI_API_KEY")
    │   └── 未找到 → ValueError
    │
    ├── 5. _apply_database_defaults() — 数据库默认值
    │   └── 缺少 database 部分时填充 backend=sqlite, sqlite_dir=.deer-flow/data
    │
    ├── 6. ExtensionsConfig.from_file() — 加载扩展配置
    │   └── 从 extensions_config.json 独立加载
    │
    ├── 7. model_validate() — Pydantic 校验
    │
    └── 8. _apply_singleton_configs() — 分发到子系统单例
        ├── load_title_config_from_dict()
        ├── load_summarization_config_from_dict()
        ├── load_memory_config_from_dict()
        ├── load_subagents_config_from_dict()
        ├── load_guardrails_config_from_dict()
        ├── load_checkpointer_config_from_dict()
        ├── load_stream_bridge_config_from_dict()
        └── load_acp_config_from_dict()
        └── checkpointer 变更 → reset_checkpointer() + reset_store()
```

## 缓存与热更新

### 全局状态

```python
_app_config: AppConfig | None          # 缓存的配置实例
_app_config_path: Path | None          # 上次加载的文件路径
_app_config_mtime: float | None        # 上次加载的文件 mtime
_app_config_is_custom: bool            # 是否通过 set_app_config 注入
```

### get_app_config() 决策流程

```
get_app_config()
    │
    ├── ContextVar 覆盖存在？
    │   └── 是 → 返回覆盖配置（最高优先级）
    │
    ├── 自定义配置（set_app_config 注入）？
    │   └── 是 → 返回自定义配置（不自动刷新）
    │
    ├── 解析当前 config.yaml 路径和 mtime
    │
    ├── 路径或 mtime 变更？
    │   ├── 是 → 重新加载
    │   └── 否 → 返回缓存
    │
    └── 返回 AppConfig 实例
```

### 为什么用 mtime 而非 TTL

- 配置未修改时不触发重新加载（零开销）
- 修改后立即生效（无需等待 TTL 过期）
- 无需选择 TTL 值

## ContextVar 覆盖栈

```
push_current_app_config(config_A)
    │
    ├── 保存当前 _current_app_config 到栈
    └── 设置 _current_app_config = config_A

push_current_app_config(config_B)
    │
    ├── 保存 config_A 到栈
    └── 设置 _current_app_config = config_B

pop_current_app_config()
    │
    ├── 从栈中恢复 config_A
    └── 设置 _current_app_config = config_A

pop_current_app_config()
    │
    ├── 栈已空
    └── 设置 _current_app_config = None（回到文件缓存模式）
```

使用场景：
- 测试中为每个测试用例注入不同配置
- LangGraph 并发执行时为不同线程使用不同配置

## 日志级别管理

`apply_logging_level(name)` 将配置中的日志级别应用到：
- `deerflow` logger
- `app` logger
- root handler（只降低，不升高）

不影响第三方库（uvicorn、sqlalchemy 等）的日志输出。

## 配置版本管理

当 config.yaml 的 schema 发生变更时：
1. 更新 config.example.yaml 中的 config_version
2. 用户启动时 `_check_config_version()` 检测版本落后
3. 发出警告，提示运行 `make config-upgrade`
4. config-upgrade 自动合并缺失的字段到用户的 config.yaml
