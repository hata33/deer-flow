# Gateway 完整生命周期

## 启动流程

### 入口点

Gateway 通过 `uvicorn` 启动，入口为 `app.gateway.app:app`：

```python
# app.py 底部
app = create_app()  # 模块级变量，uvicorn 自动加载
```

### 启动时序图

```
uvicorn 启动
  │
  ▼ create_app()
  │  ├── 创建 FastAPI 实例（配置 docs/redoc/openapi 路径）
  │  ├── 注册 AuthMiddleware
  │  ├── 注册 CSRFMiddleware
  │  ├── 注册 CORSMiddleware（仅当 GATEWAY_CORS_ORIGINS 非空）
  │  ├── 挂载所有 Router
  │  └── 注册 /health 端点
  │
  ▼ lifespan() 启动阶段（async with 上下文）
  │
  ├── 1. 配置加载
  │     ├── get_app_config() → 解析 config.yaml
  │     └── apply_logging_level() → 设置日志级别
  │
  ├── 2. LangGraph 运行时初始化
  │     └── langgraph_runtime(app)（AsyncExitStack）
  │         ├── make_stream_bridge() → StreamBridge（SSE 事件桥接）
  │         ├── init_engine_from_config() → 数据库引擎初始化
  │         │     └── 自动创建数据库（postgres 后端）
  │         ├── make_checkpointer() → 检查点存储
  │         ├── make_store() → LangGraph 键值存储
  │         ├── RunRepository + FeedbackRepository（SQL 或内存）
  │         ├── make_thread_store() → 线程元数据存储
  │         ├── make_run_event_store() → 运行事件存储
  │         └── RunManager → 运行管理器
  │
  ├── 3. 管理员引导检查
  │     └── _ensure_admin_user(app)
  │         ├── admin_count == 0 → 首次启动，输出提示
  │         └── admin_count > 0 → 迁移孤立线程到管理员
  │
  ├── 4. IM 通道服务启动
  │     └── start_channel_service(config)
  │         └── 初始化配置的 Feishu/Slack/Telegram/DingTalk 通道
  │
  └── 5. yield —— 服务就绪，开始接受请求
```

### 配置加载详情

```
get_app_config()
  │
  ├── 查找配置文件（优先级从高到低）：
  │   1. 显式 config_path 参数
  │   2. DEER_FLOW_CONFIG_PATH 环境变量
  │   3. 当前目录（backend/）的 config.yaml
  │   4. 父目录（项目根目录）的 config.yaml（推荐）
  │
  ├── 解析 YAML → AppConfig
  │     ├── models[] → 模型配置列表
  │     ├── tools[] → 工具配置列表
  │     ├── sandbox → 沙箱配置
  │     ├── memory → 记忆系统配置
  │     ├── database → 数据库连接配置
  │     └── …
  │
  ├── 缓存结果
  └── 注册 mtime 监测（自动热更新）
```

### 数据库初始化详情

```
init_engine_from_config(database_config)
  │
  ├── 创建异步 SQLAlchemy 引擎
  ├── 创建 async_session_factory
  ├── 执行 create_all() → 自动创建缺失的表
  │     ├── users → 用户表
  │     ├── threads_meta → 线程元数据表
  │     ├── runs → 运行记录表
  │     ├── run_events → 运行事件表
  │     └── feedback → 反馈表
  │
  └── postgres 后端：自动创建数据库（如不存在）
```

## 请求处理

### 完整请求路径

```
客户端（浏览器/IM/CLI）
  │
  ▼ HTTP 请求
  │
  Nginx（端口 2026）
  │
  ├── 路径匹配：
  │   ├── /api/langgraph/* → 重写为 /api/* → proxy_pass 8001
  │   ├── /api/*           → proxy_pass 8001
  │   └── 其他             → proxy_pass 3000（前端）
  │
  ▼ Gateway（端口 8001）
  │
  ├── CORSMiddleware
  │   ├── OPTIONS 预检 → 200（带 CORS 头）
  │   └── 实际请求 → 注入 CORS 头后继续
  │
  ├── CSRFMiddleware
  │   ├── 安全方法（GET/HEAD） → 跳过
  │   ├── 认证端点 → Origin 检查
  │   └── 其他变更方法 → Cookie + Header 双重验证
  │
  ├── AuthMiddleware
  │   ├── 白名单路径 → 跳过
  │   ├── 内部 Token → 合成用户
  │   └── JWT Cookie → 完整验证链
  │
  ├── FastAPI 路由分发
  │   ├── URL 匹配 → 找到处理函数
  │   └── 依赖注入 → 解析路径参数/查询参数/请求体
  │
  ├── @require_permission 装饰器
  │   ├── 权限检查（resource:action）
  │   └── 所有权检查（owner_check）→ thread_store.check_access()
  │
  ├── 路由处理函数
  │   ├── 通过 deps.py 获取单例：
  │   │   ├── get_config(request) → AppConfig
  │   │   ├── get_stream_bridge(request) → StreamBridge
  │   │   ├── get_run_manager(request) → RunManager
  │   │   ├── get_checkpointer(request) → Checkpointer
  │   │   ├── get_run_store(request) → RunStore
  │   │   ├── get_feedback_repo(request) → FeedbackRepository
  │   │   ├── get_run_event_store(request) → RunEventStore
  │   │   └── get_thread_store(request) → ThreadMetaStore
  │   │
  │   ├── 调用 services.py 业务逻辑（运行相关）
  │   │   ├── start_run() → 创建运行 + 启动后台任务
  │   │   ├── sse_consumer() → SSE 事件流生成
  │   │   ├── format_sse() → SSE 帧格式化
  │   │   └── build_run_config() → 构建运行配置
  │   │
  │   └── 调用 harness 层（deerflow.*）
  │       ├── run_agent() → 异步执行 Agent
  │       ├── make_lead_agent() → 创建 Agent 图
  │       └── 各工具/中间件/记忆系统
  │
  ▼ 响应构建
  │
  ├── Pydantic 模型序列化 → JSON
  ├── CSRFMiddleware → 认证端点设置 CSRF Cookie
  └── AuthMiddleware → finally 块清理 contextvar
  │
  ▼ HTTP 响应返回客户端
```

### SSE 流式响应路径

```
POST /api/threads/{id}/runs/stream
  │
  ├── start_run(body, thread_id, request)
  │   ├── 验证模型名称（在允许列表中）
  │   ├── RunManager.create_or_reject()
  │   │   ├── 多任务策略检查 → ConflictError → 409
  │   │   └── 创建 RunRecord
  │   ├── upsert thread metadata
  │   ├── resolve_agent_factory() → make_lead_agent
  │   ├── normalize_input() → 转换消息格式
  │   ├── build_run_config() → 构建配置
  │   ├── merge_run_context_overrides() → 合并上下文覆盖
  │   ├── inject_authenticated_user_context() → 注入用户信息
  │   └── asyncio.create_task(run_agent(…)) → 后台任务
  │
  ├── StreamingResponse(sse_consumer(…))
  │   └── 异步生成器：
  │       ├── bridge.subscribe(run_id) → 订阅事件
  │       ├── HEARTBEAT_SENTINEL → ": heartbeat\n\n"
  │       ├── END_SENTINEL → event: end
  │       └── 其他事件 → format_sse(event, data)
  │
  └── 断开连接处理（finally 块）：
      ├── on_disconnect == "cancel" → 取消后台任务
      └── on_disconnect == "continue" → 任务继续运行
```

## 关闭流程

### 关闭触发

```
信号（SIGTERM/SIGINT）→ uvicorn
  │
  ▼ FastAPI lifespan 退出（退出 async with 上下文）
  │
  ├── 1. 停止 IM 通道服务
  │     └── stop_channel_service()
  │         ├── 逐个停止通道（Feishu/Slack/Telegram/DingTalk）
  │         └── 超时保护：5 秒（_SHUTDOWN_HOOK_TIMEOUT_SECONDS）
  │             ├── 超时 → 警告日志，继续关闭
  │             └── 失败 → 异常日志，继续关闭
  │
  ├── 2. AsyncExitStack 清理（逆序）
  │     ├── RunManager → 停止运行管理
  │     ├── RunEventStore → 关闭事件存储
  │     ├── ThreadMetaStore → 关闭线程存储
  │     ├── Store → 关闭键值存储
  │     ├── Checkpointer → 关闭检查点存储
  │     └── StreamBridge → 关闭事件桥接
  │
  ├── 3. 关闭持久化引擎
  │     └── close_engine()
  │         └── 关闭 SQLAlchemy 异步引擎
  │
  └── 4. 日志输出
      └── "Shutting down API Gateway"
```

### 关闭超时保护

```python
_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0

try:
    await asyncio.wait_for(
        stop_channel_service(),
        timeout=_SHUTDOWN_HOOK_TIMEOUT_SECONDS,
    )
except TimeoutError:
    logger.warning(
        "Channel service shutdown exceeded %.1fs; proceeding with worker exit.",
        _SHUTDOWN_HOOK_TIMEOUT_SECONDS,
    )
```

这确保即使通道服务关闭卡住，uvicorn 的 reload 监管器也不会一直等待。

## 配置热更新

### config.yaml 热更新

`get_app_config()` 实现了自动的配置热更新：

```python
def get_app_config() -> AppConfig:
    # 缓存解析后的配置
    # 自动检测：
    #   1. 配置文件路径是否变化
    #   2. 文件 mtime 是否增长
    # 满足任一条件则重新解析
```

**触发条件**：
- 配置文件路径变化（如环境变量 `DEER_FLOW_CONFIG_PATH` 被修改）
- 文件 mtime 增长（文件被编辑）

**应用范围**：
- Gateway 和 LangGraph 读取保持对齐
- 无需手动重启进程

### extensions_config.json 热更新

MCP 和技能配置通过 API 接口修改后自动生效：

```
PUT /api/mcp/config
  ├── 保存到 extensions_config.json
  ├── reload_extensions_config() → 重载内存缓存
  └── LangGraph 运行时检测 mtime 变化 → 重新初始化 MCP 工具
```

### Agent 系统提示词缓存刷新

```
PUT /api/skills/{name}
  ├── 更新 extensions_config.json
  └── refresh_skills_system_prompt_cache_async() → 清除缓存
```

## 错误处理

### HTTP 异常映射

| 异常类型 | HTTP 状态码 | 场景 |
|----------|-------------|------|
| `HTTPException(401)` | 401 | 认证失败 |
| `HTTPException(403)` | 403 | CSRF 检查失败 / 权限不足 |
| `HTTPException(404)` | 404 | 资源不存在 / 所有权检查失败 |
| `HTTPException(409)` | 409 | 并发冲突 / 名称已存在 |
| `HTTPException(422)` | 422 | 请求参数验证失败 |
| `HTTPException(429)` | 429 | 速率限制 |
| `HTTPException(500)` | 500 | 内部服务器错误 |
| `HTTPException(501)` | 501 | 功能未实现（OAuth） |
| `HTTPException(503)` | 503 | 服务不可用（依赖未就绪） |

### 认证错误码

```json
{
  "detail": {
    "code": "invalid_credentials",
    "message": "Incorrect email or password"
  }
}
```

完整错误码列表：
- `not_authenticated` — 未认证
- `invalid_credentials` — 凭据错误
- `token_expired` — Token 过期
- `token_invalid` — Token 无效
- `user_not_found` — 用户不存在
- `email_already_exists` — 邮箱已注册
- `system_already_initialized` — 系统已初始化

### 日志记录策略

| 级别 | 使用场景 |
|------|----------|
| `DEBUG` | 非关键操作失败（如线程元数据删除失败） |
| `INFO` | 正常操作（配置加载、线程创建、Agent 创建） |
| `WARNING` | 非致命问题（密码重哈希失败、通道启动失败、配置过期） |
| `ERROR` | 操作失败（技能加载失败、代理创建失败） |
| `EXCEPTION` | 带堆栈跟踪的异常（用于调试） |

### 503 服务不可用

当 `deps.py` 中的单例获取器检测到依赖未初始化时返回 503：

```python
def _require(attr, label):
    def dep(request):
        val = getattr(request.app.state, attr, None)
        if val is None:
            raise HTTPException(
                status_code=503,
                detail=f"{label} not available"
            )
        return val
    return dep
```

这确保在启动过程中或异常状态下，请求不会因为 `None` 引用而产生 500 错误。

## 孤立线程迁移

### 触发时机

每次 Gateway 启动且管理员已存在时执行。

### 迁移流程

```
_migrate_orphaned_threads(store, admin_user_id)
  │
  ├── 分页遍历 LangGraph store 中的 threads 命名空间
  │   └── _iter_store_items(store, ("threads",), page_size=500)
  │       ├── 游标式分页（offset += page_size）
  │       └── 短页（< page_size）时终止
  │
  ├── 检查每条线程的 metadata.user_id
  │   └── 为空 → 设置为 admin_user_id
  │
  └── store.aput() → 更新线程元数据
```

### 用途

覆盖"无认证 → 有认证"的升级路径：用户在未启用认证的情况下运行 DeerFlow 时产生的 LangGraph 线程数据需要有归属。
