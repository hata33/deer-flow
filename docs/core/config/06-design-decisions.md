# 06 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **YAML 格式 + Pydantic 校验** | 人类可读 + 类型安全，兼得声明式和运行时校验 |
| 2 | **config_version 字段 + 启动比对** | 检测配置漂移，提醒用户升级新增字段 |
| 3 | **mtime 缓存 + 自动重载** | 编辑 config.yaml 后无需重启 Gateway |
| 4 | **$ENV_VAR 前缀解析** | 密钥不落盘，配置文件可安全提交 Git |
| 5 | **多级配置搜索路径** | 灵活适配本地开发、Docker、CI 等不同部署模式 |
| 6 | **harness/app 严格导入边界** | harness 是可发布包，不能依赖未发布的 app 层 |

---

## 二、逐决策分析

### 决策 1：YAML 格式 + Pydantic 校验

**问题**：配置文件如何兼顾人类可读性和程序安全性？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 纯 Python 配置 | 灵活 | 用户可能引入任意代码执行 |
| JSON | 通用 | 不支持注释，嵌套层级深时难读 |
| TOML | 简单 | 复杂嵌套结构表达力不足 |
| **YAML + Pydantic（当前）** | 注释友好 + 类型校验 | YAML 解析有安全风险（需 safe_load） |

**选择 YAML + Pydantic**：YAML 支持注释（用户可以记录为什么这样配置），`yaml.safe_load()` 防止代码注入，Pydantic 的 `model_validate()` 提供类型检查和默认值填充。

`extra="allow"` 让 Pydantic 不拒绝未知字段——不同 Provider 有自己的特定参数（如 `enable_prompt_caching`、`max_retries`），不需要为每个 Provider 在配置系统中定义专用字段。这些额外字段通过 `model_dump()` 直接透传到 Provider 构造函数。

---

### 决策 2：config_version 字段 + 启动比对

**问题**：DeerFlow 持续迭代，`config.yaml` 会新增字段。用户升级代码后可能不知道需要更新配置，导致新功能静默失败。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 无版本管理 | 简单 | 用户不知道配置过时 |
| **版本号比对 + 警告（当前）** | 非侵入式，不阻断启动 | 依赖 config.example.yaml 存在 |
| 严格版本校验 | 确保一致性 | 阻断升级，影响用户体验 |

**选择比对 + 警告**：`_check_config_version()` 将用户 `config.yaml` 的 `config_version` 与 `config.example.yaml` 的版本比较。落后时输出 `WARNING` 日志并提示 `make config-upgrade`。缺失 `config_version` 视为版本 0（版本化之前的配置）。

搜索 `config.example.yaml` 时向上最多 5 级目录，覆盖项目根目录和 backend/ 目录的各种相对位置。

---

### 决策 3：mtime 缓存 + 自动重载

**问题**：`get_app_config()` 在 Gateway 的每个请求中被调用（中间件、Agent 构建、工具加载），如果每次都从磁盘读取会严重影响性能。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 固定 TTL 缓存 | 简单 | 延迟感知，可能用过时配置 |
| **mtime 比对（当前）** | 实时感知文件变更 | 每次 get_app_config() 调一次 stat() |
| 信号监听（inotify） | 最精确 | 平台依赖，增加复杂度 |

**选择 mtime 比对**：`get_app_config()` 每次调用时获取文件的 `st_mtime`，与缓存中的比较。变更时自动触发 `from_file()` 重新加载。开销是一次 `stat()` 系统调用（微秒级），远小于 YAML 解析 + Pydantic 校验（毫秒级）。

三个条件触发重载：
1. `_app_config is None`：首次加载
2. `_app_config_path != resolved_path`：配置路径变更（环境变量改变）
3. `_app_config_mtime != current_mtime`：文件被编辑

自定义配置（通过 `set_app_config()` 注入）不会被自动刷新，避免测试中的意外覆盖。

---

### 决策 4：$ENV_VAR 前缀解析

**问题**：API Key、OAuth Secret 等敏感信息不能明文写在配置文件中。

| 方案 | 优势 | 劣势 |
|------|------|------|
| .env 文件自动注入 | 对用户透明 | 不明确哪些值需要环境变量 |
| **$ 前缀显式标记（当前）** | 一目了然，配置可提交 | 变量不存在时运行时报错 |
| Jinja2 模板 | 灵活 | 过于复杂 |

**选择 $ 前缀**：`resolve_env_variables()` 递归遍历所有配置值，遇到 `$` 开头的字符串调用 `os.getenv()`。变量不存在时抛出 `ValueError`——配置错误应尽早发现（fail-fast），不应静默使用 None 值。

两个配置文件有独立的解析实现：
- `AppConfig.resolve_env_variables()`：纯函数，返回新 dict，不修改原始配置
- `ExtensionsConfig.resolve_env_variables()`：就地修改 dict（in-place），未找到时替换为空字符串（避免下游收到字面 `$VAR`）

行为差异的原因：`ExtensionsConfig` 的环境变量主要出现在 MCP 服务器的 `env` 字段中，缺失变量不应阻断整个配置加载。

---

### 决策 5：多级配置搜索路径

**问题**：DeerFlow 支持多种部署模式，配置文件位置不固定：

| 部署模式 | config.yaml 位置 |
|----------|-----------------|
| 本地开发（项目根目录） | `deer-flow/config.yaml` |
| 本地开发（backend 目录） | `deer-flow/backend/config.yaml` |
| Docker Compose | 容器内挂载路径 |
| CI/CD | 任意路径 |

| 方案 | 优势 | 劣势 |
|------|------|------|
| 固定路径 | 确定性 | 不灵活 |
| **多级搜索（当前）** | 兼容各种部署 | 搜索顺序必须稳定 |
| 只支持环境变量 | 最灵活 | 用户必须设置环境变量 |

**选择多级搜索**：

```
优先级：
1. 显式参数 config_path
2. DEER_FLOW_CONFIG_PATH 环境变量
3. 项目根目录下的 config.yaml（existing_project_file）
4. 传统 monorepo 位置（backend/ 和 repo-root/）
5. 都找不到 → FileNotFoundError
```

`existing_project_file()` 基于 `project_root()`（CWD 或 `DEER_FLOW_PROJECT_ROOT`），确保在 Docker 和本地开发中都能正确定位。传统 monorepo 位置的 `_legacy_config_candidates()` 作为向后兼容，搜索 `__file__` 的上级目录。

---

### 决策 6：harness/app 严格导入边界

**问题**：harness（`packages/harness/deerflow/`）是可发布的独立包，而 app（`app/`）是未发布的 Gateway 应用层。如果 harness 导入 app，包发布后会因缺少 app 而崩溃。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 无边界 | 简单 | 循环依赖、不可发布 |
| **单向导入 + CI 强制（当前）** | 清晰分层 | 需要持续维护 |

**选择严格单向**：app 可以 `from deerflow.config import get_app_config`，但 deerflow 不能 `from app.gateway import ...`。`tests/test_harness_boundary.py` 在 CI 中扫描所有 `deerflow/` 下的 Python 文件，检测任何 `from app` 或 `import app` 的导入，确保边界不被违反。

`_apply_singleton_configs()` 的存在是因为部分子系统（记忆、标题、摘要）尚未迁移到显式 `AppConfig` 传递，仍依赖全局单例。这些单例由 `from_file()` 加载时分发。注释明确标注："新代码应优先直接传递 AppConfig 实例"。

---

## 三、设计效果

| 效果 | 实现方式 |
|------|----------|
| **零重启热更新** | mtime 变更自动重载，编辑 config.yaml 即生效 |
| **密钥安全** | `$VAR` 前缀标记，配置文件可安全提交 |
| **配置漂移检测** | 版本比对 + 警告日志 + `make config-upgrade` 升级工具 |
| **多部署兼容** | 4 级搜索路径覆盖本地、Docker、CI 等场景 |
| **类型安全** | Pydantic 校验 + `extra="allow"` Provider 透传 |
| **协程安全** | ContextVar 覆盖栈支持测试和运行时配置切换 |
| **可发布** | harness/app 边界 + CI 强制检测 |
