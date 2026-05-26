# Gateway 实现分析

> 本文档基于源码逐层拆解实现细节。回答"代码怎么写的、为什么这么写"。

## 模块依赖图

```
app.py (应用工厂 + 生命周期)
 ├── config.py (GatewayConfig 单例)
 ├── auth_middleware.py (AuthMiddleware — 认证门控)
 ├── csrf_middleware.py (CSRFMiddleware — Double Submit Cookie)
 │     └── _configured_cors_origins() ←── GATEWAY_CORS_ORIGINS 环境变量
 ├── deps.py (依赖注入枢纽 + langgraph_runtime)
 │     ├── make_stream_bridge() → StreamBridge
 │     ├── make_checkpointer() → Checkpointer
 │     ├── make_store() → LangGraph Store
 │     ├── RunRepository / FeedbackRepository / ThreadMetaStore
 │     ├── make_run_event_store() → RunEventStore
 │     └── RunManager(store=RunStore)
 ├── routers/ (14 个路由模块)
 │     ├── thread_runs.py ←── services.py (核心业务逻辑)
 │     ├── runs.py ←── services.py (无状态运行)
 │     ├── agents.py, artifacts.py, channels.py, feedback.py
 │     ├── mcp.py, memory.py, models.py, skills.py
 │     ├── suggestions.py, threads.py, uploads.py
 │     ├── auth.py (JWT 认证端点)
 │     └── assistants_compat.py (LangGraph Platform 桩)
 ├── internal_auth.py (进程内认证令牌)
 └── authz.py (权限检查装饰器)
```

---

## 第 1 层: 应用构建与中间件栈

### create_app() — 应用工厂

`app.py:create_app()` 按 FastAPI 惯例构建应用实例。关键步骤：

1. **文档端点控制**: `GATEWAY_ENABLE_DOCS=false` 时将 `docs_url`、`redoc_url`、`openapi_url` 设为 `None`，生产环境隐藏 API 文档
2. **中间件注册顺序**（Starlette 的执行顺序是**反向注册**）:
   - `AuthMiddleware` — 最先注册，最后执行（外层），拦截所有未认证请求
   - `CSRFMiddleware` — 中间层，对状态变更方法做 Token 校验
   - `CORSMiddleware` — 最后注册，最先执行（内层），处理预检请求
3. **路由挂载**: 14 个 `include_router` 调用，每个模块带独立的 `prefix` 和 `tags`

### lifespan() — 生命周期管理

使用 `@asynccontextmanager` 管理完整的启动/关闭周期：

```
启动:
  config.yaml → app.state.config
  → langgraph_runtime(app)       # AsyncExitStack 管理所有单例
    → _ensure_admin_user(app)     # 首次引导 / 孤儿线程迁移
    → start_channel_service()     # IM 频道（可选）
  → yield                         # 服务就绪

关闭:
  → stop_channel_service()        # 超时 5s 保护
  → close_engine()                # 关闭数据库引擎
```

孤儿线程迁移（`_migrate_orphaned_threads`）处理"无认证升级到有认证"场景：将 LangGraph Store 中没有 `user_id` 的线程归属到管理员账号。使用游标分页（`_iter_store_items`）确保大量孤儿不会被静默丢失。

---

## 第 2 层: 依赖注入与运行时初始化

### deps.py — 依赖注入枢纽

**`_require()` 工厂模式**: 为每个运行时单例生成类型安全的 FastAPI 依赖函数。当单例不可用时返回 `503 Service Unavailable`，而非 `None` 导致下游 `AttributeError`。

```python
get_stream_bridge = _require("stream_bridge", "Stream bridge")
get_run_manager   = _require("run_manager", "Run manager")
# ... 批量生成
```

**`langgraph_runtime()` 初始化链**: 使用 `AsyncExitStack` 确保初始化失败时已创建的资源被正确清理。顺序依赖：
- `StreamBridge` 先创建（Agent 运行需要发布事件）
- 持久化引擎在 Checkpointer 之前初始化（PostgreSQL 自动建库）
- `RunManager` 最后创建（需要 `RunStore` 实例）

**认证辅助函数**: `get_local_provider()` 使用模块级缓存避免每次请求重建 `LocalAuthProvider` 和 `SQLiteUserRepository`。必须延迟到 `init_engine_from_config()` 之后调用。

---

## 第 3 层: 运行生命周期

### services.py — 核心业务逻辑

运行生命周期是 Gateway 的核心数据流：

```
HTTP 请求 → start_run(body, thread_id, request)
  → RunManager.create_or_reject()     # 并发冲突检测
  → thread_store.create/update_status  # 线程元数据 upsert
  → build_run_config()                # 组装 RunnableConfig
  → merge_run_context_overrides()     # 白名单上下文合并
  → inject_authenticated_user_context()  # 注入用户身份
  → asyncio.create_task(run_agent())  # 后台启动 Agent
  → 返回 RunRecord

SSE 流 → sse_consumer(bridge, record, request, run_mgr)
  → bridge.subscribe(run_id)          # 订阅事件流
  → HEARTBEAT_SENTINEL → ": heartbeat\n\n"
  → END_SENTINEL → format_sse("end", None)
  → 正常事件 → format_sse(event, data)
  finally:
    → on_disconnect == cancel → run_mgr.cancel()
```

### format_sse() — SSE 帧格式

严格遵循 LangGraph Platform 线格式，字段顺序为 `event:` -> `data:` -> `id:`（可选） -> 空行。此格式被 `useStream` React Hook 和 Python `langgraph-sdk` SSE 解码器直接消费。

### build_run_config() — 配置构建

处理 LangGraph 版本兼容性：
- LangGraph >= 0.6.0 引入 `context` 字段，与 `configurable` 互斥
- 两者同时存在时优先使用 `context`（新版语义）
- 自定义 Agent 通过 `agent_name` 键在 `configurable/context` 中路由，`make_lead_agent` 读取该键加载对应的 `SOUL.md`

**上下文白名单**（`_CONTEXT_CONFIGURABLE_KEYS`）: 只转发 Agent 相关参数（`model_name`、`thinking_enabled`、`agent_name` 等），防止注入无关参数。

---

## 第 4 层: SSE 消费者

### sse_consumer() — 异步生成器

核心循环：从 `StreamBridge` 订阅事件并生成 SSE 帧。

1. 支持 `Last-Event-ID` 恢复（从断点继续消费）
2. 每次迭代检查 `request.is_disconnected()`（客户端断开检测）
3. `HEARTBEAT_SENTINEL` 转换为 SSE 注释帧（`": heartbeat\n\n"`），保持连接活跃
4. `END_SENTINEL` 触发 `format_sse("end", None)` 并 return

**on_disconnect 语义**: `finally` 块根据 `DisconnectMode` 决定是否取消后台 Agent 任务。`cancel` 模式适合 Web UI（用户关闭页面则停止推理），`continue` 模式适合 API 客户端（断开后任务继续执行）。

## 第 5 层: 路由组织与端点模式

14 个路由模块按职责域划分：CRUD 资源（`threads`、`agents`、`skills`、`models`）、运行生命周期（`thread_runs`、`runs`）、文件操作（`artifacts`、`uploads`）、认证（`auth`）、辅助功能（`mcp`、`memory`、`suggestions`、`feedback`、`channels`）和兼容层（`assistants_compat`）。

**权限检查**: 所有端点通过 `@require_permission(resource, action, owner_check=True)` 统一校验。`AuthMiddleware` 作为全局安全网实现 fail-closed 模型——即使路由没有显式权限装饰器，中间件也会拦截未认证请求。

**认证端点特殊处理**: `/login`、`/register`、`/initialize` 标记为 `_AUTH_EXEMPT_PATHS`，免于 Double Submit Token 检查，但仍需通过 Origin 校验。

**内部认证通道**: `internal_auth.py` 为同进程的 IM 频道 Worker 提供进程级令牌（`X-DeerFlow-Internal-Token`），`AuthMiddleware` 识别后跳过 JWT 校验。内部用户拥有 `DEFAULT_USER_ID` 和 `system_role="internal"`。
