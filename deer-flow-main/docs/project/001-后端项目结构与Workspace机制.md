# 001 - 后端项目结构与 Workspace 机制

## 概览

DeerFlow 后端采用 **uv workspace（monorepo）** 结构，将 HTTP 网关层与核心能力包分离：

```
backend/
├── pyproject.toml              # 主项目配置，定义 workspace
├── app/                        # FastAPI 网关应用
│   └── gateway/
│       ├── app.py              # 应用入口（create_app + uvicorn）
│       ├── deps.py             # 依赖注入（单例获取器）
│       ├── routers/            # 各业务路由
│       └── config.py           # 网关配置
└── packages/
    └── harness/
        ├── pyproject.toml      # 子包配置（deerflow-harness）
        └── deerflow/           # 实际的 Python 包源码
            ├── config/         # 配置模块
            ├── agents/         # Agent 构建、检查点等
            ├── runtime/        # StreamBridge、RunManager、Store
            ├── tools/          # 工具系统
            ├── skills/         # 技能系统
            └── ...
```

## 两个 pyproject.toml 的关系

### 主项目 `backend/pyproject.toml`

声明 workspace 成员和依赖来源：

```toml
[tool.uv.workspace]
members = ["packages/harness"]

[tool.uv.sources]
deerflow-harness = { workspace = true }
```

- `members`：指定 `packages/harness` 是 workspace 子包
- `uv.sources`：将 `deerflow-harness` 依赖指向 workspace 本地，而非 PyPI

### 子包 `packages/harness/pyproject.toml`

定义包的打包规则：

```toml
[project]
name = "deerflow-harness"

[tool.hatch.build.targets.wheel]
packages = ["deerflow"]
```

- 包名为 `deerflow-harness`
- 打包时将 `packages/harness/deerflow/` 目录作为 `deerflow` 模块

## import 解析路径

当你在 `app.py` 中看到：

```python
from deerflow.config.app_config import get_app_config
from deerflow.runtime import RunManager, StreamBridge
```

这些 `deerflow.*` 导入解析到的物理路径是：

```
deerflow.config.app_config  →  packages/harness/deerflow/config/app_config.py
deerflow.runtime             →  packages/harness/deerflow/runtime/__init__.py
deerflow.agents.*            →  packages/harness/deerflow/agents/
```

**原理**：`uv sync` 时，`deerflow-harness` 以 editable 模式安装到虚拟环境中，Python 解释器将 `deerflow` 映射到 `packages/harness/deerflow/` 目录。修改该目录下的代码后无需重新安装即可生效。

## app 入口做了什么

`app.py` 的 `create_app()` 函数创建 FastAPI 实例，`lifespan` 管理生命周期：

1. **启动阶段**
   - 加载应用配置（`get_app_config()`）
   - 初始化 LangGraph runtime（`langgraph_runtime(app)`）：
     - `StreamBridge` — SSE 流式推送
     - `Checkpointer` — 对话状态持久化
     - `Store` — 跨线程存储
     - `RunManager` — 运行管理器
   - 启动 IM 渠道服务（飞书、Slack、Telegram）

2. **请求处理** — 各 router 通过 `deps.py` 中的 getter 函数获取单例

3. **关闭阶段** — 停止渠道服务，释放 runtime 资源

## 为什么这样设计

| 优势 | 说明 |
|------|------|
| **关注点分离** | 网关层（HTTP/路由）与核心逻辑（Agent/工具/配置）独立演进 |
| **可复用** | `deerflow-harness` 包可被其他项目直接引用 |
| **开发体验** | editable 安装，修改即刻生效，无需重新构建 |
| **独立版本** | 子包有自己的版本号和依赖声明，便于独立发布 |
