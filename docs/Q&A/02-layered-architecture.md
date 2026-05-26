# Q&A 02: 分层架构

> 整个系统的分层架构是如何设计的？各层的职责和边界是什么？

---

## 六层架构总览

```
┌───────────────────────────────────────────────────────────┐
│                    Frontend Layer                          │
│              Next.js 16 · Port 3000                       │
├───────────────────────────────────────────────────────────┤
│                    Gateway API Layer                       │
│              FastAPI · Port 8001                          │
├───────────────────────────────────────────────────────────┤
│                 Agent Orchestration Layer                  │
│          LangGraph StateGraph · Port 2024                 │
├──────────┬──────────────┬──────────────┬──────────────────┤
│ Runtime  │    Model     │    Tool      │    Sandbox       │
│  Layer   │    Layer     │    Layer     │    Layer         │
├──────────┴──────────────┴──────────────┴──────────────────┤
│                 Configuration Layer                        │
│              YAML + Pydantic + 反射加载                    │
└───────────────────────────────────────────────────────────┘
```

---

## 各层职责与边界

### 1. Frontend Layer（`frontend/`）

**职责**: 用户界面和客户端状态管理。

**核心能力**:
- SSE 流式对话渲染
- 消息三源合并（历史 + 流 + 乐观）
- 文件上传与预览
- 国际化（中/英）

**边界**: 通过 HTTP 与 Gateway 通信。SSE 流通过 `/api/langgraph/*` 代理到 LangGraph。非 Agent 操作（上传、模型列表）直接调 Gateway。

**关键入口**: `frontend/src/app/workspace/chats/[thread_id]/page.tsx`

---

### 2. Gateway API Layer（`backend/app/gateway/`）

**职责**: REST API 网关。处理鉴权、CSRF、CORS、文件管理、线程管理。

**核心路由**:

| 路由 | 职责 |
|------|------|
| `/api/langgraph/*` | 反向代理到 LangGraph Server |
| `/api/threads/{id}/uploads` | 文件上传 |
| `/api/threads/{id}/runs/{rid}/messages` | 历史消息加载 |
| `/api/models` | 模型列表 |
| `/api/skills` | 技能管理 |
| `/api/mcp` | MCP 服务器配置 |
| `/api/artifacts` | Artifact 文件服务 |

**边界**: 上层接受 Frontend 请求；下层将 Agent 操作转发到 LangGraph，非 Agent 操作自行处理。

**关键入口**: `backend/app/gateway/app.py`

**中间件栈**: Auth → CSRF → CORS（严格顺序）

---

### 3. Agent Orchestration Layer（`backend/packages/harness/deerflow/agents/`）

**职责**: Agent 工作流编排。中间件链、ReAct 循环、子代理调度。

**核心组件**:

| 组件 | 职责 |
|------|------|
| `lead_agent/` | 主 Agent 工厂（构建 StateGraph + 绑定中间件） |
| `middlewares/` | 20 个中间件（8 基础 + 12 条件），6 个生命周期钩子 |
| `thread_state.py` | LangGraph 状态 schema 扩展 |
| `factory.py` | Agent 工厂入口 |

**中间件链（两层组装）**:

```
build_lead_runtime_middlewares() — 8 个基础中间件（固定顺序）
    ↓
_build_middlewares() — 最多 12 个条件中间件（按配置启用）
```

**边界**: 上层被 LangGraph Runtime 或 Gateway 调用；下层调用 Runtime（状态持久化）、Model（LLM）、Tool（工具执行）。

**关键入口**: `agents/lead_agent/agent.py` → `make_lead_agent()`

---

### 4. Runtime Layer（`backend/packages/harness/deerflow/runtime/`）

**职责**: 运行时管理。Run 生命周期、流式传输、状态持久化、事件发布。

**核心组件**:

| 组件 | 职责 |
|------|------|
| `runs/manager.py` | Run 注册、并发控制、取消/超时 |
| `runs/worker.py` | 异步执行 Agent 并通过 StreamBridge 推送事件 |
| `stream_bridge/` | SSE 事件发布/订阅（发布-订阅模式） |
| `checkpointer/` | 检查点持久化（SQLite/PostgreSQL） |
| `store/` | 运行时数据存储 |

**边界**: 为 Agent 编排层提供执行环境和持久化服务。对上层透明——Agent 只需调用 `agent.astream()`，RunManager 和 StreamBridge 自动处理其余逻辑。

**关键入口**: `runtime/runs/worker.py` → `run_agent()`

---

### 5. Model Layer（`backend/packages/harness/deerflow/models/`）

**职责**: LLM 抽象和工厂模式。统一多种 LLM Provider 的调用接口。

**核心能力**:
- **反射工厂**: 通过 `resolve_variable("module:Class")` 动态加载模型类
- **Thinking 模式**: 支持 DeepSeek、Claude 等模型的推理增强
- **Vision 支持**: 条件性注入图像处理工具
- **Provider**: OpenAI、Anthropic、DeepSeek、Codex CLI、vLLM 等

**边界**: 只被 Agent 编排层调用。返回 `BaseChatModel` 实例，Agent 无需感知底层差异。

**关键入口**: `models/factory.py` → `create_model()`

---

### 6. Tool Layer（`backend/packages/harness/deerflow/tools/`）

**职责**: 工具加载、组装和管理。

**工具来源（五种）**:

| 来源 | 路径 | 加载方式 |
|------|------|---------|
| 内置工具 | `tools/builtins/` | 硬编码注册 |
| 配置工具 | `config.yaml` | `@tool` + 反射加载 |
| MCP 工具 | MCP Server | 懒初始化 + mtime 缓存 |
| 社区工具 | `community/` | `@tool` 标准注册 |
| 子代理工具 | `subagents/` | 条件启用（ultra 模式） |

**边界**: 被 Agent 编排层绑定到 LLM。工具内部可调用 Sandbox Layer 执行代码。

**关键入口**: `tools/tools.py` → `assemble_tools()`

---

### 7. Sandbox Layer（`backend/packages/harness/deerflow/sandbox/`）

**职责**: 安全执行环境。隔离代码执行，提供虚拟路径系统。

**实现**:

| 实现 | 场景 | 特点 |
|------|------|------|
| `LocalSandbox` | 开发环境 | 直接文件系统访问 |
| `AioSandbox` | 生产环境 | Docker 容器隔离 |

**虚拟路径系统**: `/mnt/user-data/...` 抽象，通过 `PathMapping` 转换为宿主路径。

---

### 8. Configuration Layer（`backend/packages/harness/deerflow/config/`）

**职责**: 配置管理和验证。贯穿所有层。

**核心特点**:
- YAML + Pydantic 校验
- mtime 热重载（配置文件修改后自动生效）
- `$ENV_VAR` 环境变量解析
- 反射模式：`resolve_variable("module.path:ClassName")`

---

## 层间调用关系

```
Frontend ──HTTP──▶ Gateway ──Proxy──▶ LangGraph Server
                                          │
                     Gateway ──async──▶ Agent Orchestration
                                          │
                                    ┌─────┼─────┐
                                    │     │     │
                                  Model  Tool  Runtime
                                              │
                                          Sandbox
```

**关键依赖规则**:
- 上层依赖下层，不允许反向依赖
- Configuration Layer 被所有层依赖（横向）
- Agent Orchestration 是核心枢纽，协调 Runtime、Model、Tool 三层

---

## 相关源码

| 层 | 入口文件 |
|----|---------|
| Gateway | `backend/app/gateway/app.py` |
| Agent | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` |
| Runtime | `backend/packages/harness/deerflow/runtime/runs/worker.py` |
| Model | `backend/packages/harness/deerflow/models/factory.py` |
| Tool | `backend/packages/harness/deerflow/tools/tools.py` |
| Config | `backend/packages/harness/deerflow/config/app_config.py` |

## 深入阅读

- [架构决策索引](../docs/guides/01-architecture-decisions.md)
- [可替换性地图](../docs/guides/03-replaceability-map.md)
- [Agent 设计决策](../docs/core/agent/06-design-decisions.md)
