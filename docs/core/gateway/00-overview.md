# Gateway 全局概览

## 系统定位

DeerFlow Gateway 是基于 **FastAPI** 构建的 REST API 服务，默认监听端口 **8001**。它承担双重职责：

1. **REST API 网关** — 向前端和 IM 通道暴露统一的 HTTP 接口
2. **嵌入式 LangGraph 运行时** — 通过 `RunManager` + `StreamBridge` + `run_agent()` 在同一进程内执行 Agent

整个系统通过 **Nginx**（端口 2026）对外提供统一入口，前端（端口 3000）通过 Nginx 代理访问 Gateway。

## 模块结构

```
backend/app/gateway/                 # Gateway 根目录（共约 41 个文件）
├── app.py                           # FastAPI 应用创建与生命周期管理
├── config.py                        # Gateway 自身配置（host/port/docs）
├── auth_middleware.py                # 全局认证中间件
├── csrf_middleware.py                # CSRF 双重提交 Cookie 中间件
├── authz.py                         # 权限装饰器（require_auth / require_permission）
├── deps.py                          # 依赖注入：单例获取器 + 认证辅助
├── internal_auth.py                 # 进程内通信认证（Channel Worker）
├── langgraph_auth.py                # LangGraph Server 兼容认证层
├── services.py                      # 运行生命周期服务层（SSE 格式化、run 启动）
├── utils.py                         # 工具函数
├── path_utils.py                    # 虚拟路径解析
│
├── auth/                            # 认证子系统
│   ├── __init__.py                  # 公共导出
│   ├── config.py                    # JWT 密钥管理
│   ├── jwt.py                       # JWT 创建与验证
│   ├── models.py                    # User / UserResponse 模型
│   ├── password.py                  # 版本化密码哈希（v1/v2）
│   ├── providers.py                 # AuthProvider 抽象接口
│   ├── local_provider.py            # 本地邮箱/密码认证实现
│   ├── errors.py                    # AuthErrorCode / TokenError 枚举
│   ├── credential_file.py           # 凭据文件写入（0600 权限）
│   ├── reset_admin.py               # CLI 管理员密码重置工具
│   └── repositories/
│       ├── base.py                  # UserRepository 抽象接口
│       └── sqlite.py                # SQLAlchemy 异步实现
│
└── routers/                         # 路由层（HTTP 接口）
    ├── agents.py                    # 自定义代理 CRUD
    ├── artifacts.py                 # 产物文件访问
    ├── assistants_compat.py         # LangGraph Assistants 兼容层
    ├── auth.py                      # 认证接口（登录/注册/登出）
    ├── channels.py                  # IM 通道状态管理
    ├── feedback.py                  # 运行反馈（点赞/点踩）
    ├── mcp.py                       # MCP 配置管理
    ├── memory.py                    # 记忆数据管理
    ├── models.py                    # 模型列表与详情
    ├── runs.py                      # 无状态运行（stream/wait）
    ├── skills.py                    # 技能管理
    ├── suggestions.py               # 跟进问题生成
    ├── thread_runs.py               # 线程运行生命周期
    ├── threads.py                   # 线程 CRUD 与状态管理
    └── uploads.py                   # 文件上传管理
```

三层架构：

| 层级 | 目录 | 职责 |
|------|------|------|
| **路由层** | `routers/` | HTTP 请求解析、参数校验、响应序列化 |
| **服务层** | `services.py` + `deps.py` | 业务逻辑编排、单例管理 |
| **核心层** | `auth/` + 中间件 | 认证授权、安全防护 |

## 请求处理链

每个 HTTP 请求经过以下处理链：

```
客户端请求
  │
  ▼
Nginx（端口 2026）
  ├── /api/langgraph/* → 重写为 /api/* → Gateway
  └── /api/*           → 直接转发      → Gateway
  │
  ▼
CORS 中间件（CORSMiddleware）
  │  检查 GATEWAY_CORS_ORIGINS 配置
  │  同源请求默认放行
  ▼
CSRF 中间件（CSRFMiddleware）
  │  状态变更请求（POST/PUT/DELETE/PATCH）
  │  验证 cookie + header 双重 CSRF token
  ▼
认证中间件（AuthMiddleware）
  │  白名单路径直接放行
  │  非白名单路径验证 JWT cookie
  │  注入 request.state.user 和 contextvar
  ▼
路由处理函数
  │  @require_permission 执行细粒度权限检查
  ▼
服务层 / Harness
  │  deps.py 获取单例（RunManager、StreamBridge 等）
  │  services.py 执行业务逻辑
  ▼
HTTP 响应
```

## Nginx 反向代理路由规则

| 路径 | 目标 | 说明 |
|------|------|------|
| `/api/langgraph/*` | Gateway `8001` → 重写为 `/api/*` | LangGraph 兼容运行时 |
| `/api/*`（其他） | Gateway `8001` | REST API |
| `/`（非 API） | Frontend `3000` | Next.js 前端 |

## 生命周期管理

Gateway 使用 FastAPI 的 `lifespan` 异步上下文管理器管理应用生命周期：

### 启动流程

```
lifespan() 启动阶段：
  1. 加载配置 → get_app_config()
  2. 设置日志级别 → apply_logging_level()
  3. 初始化 LangGraph 运行时 → langgraph_runtime(app)
     ├── StreamBridge（SSE 事件桥接）
     ├── 持久化引擎初始化 → init_engine_from_config()
     ├── Checkpointer（检查点存储）
     ├── Store（LangGraph 键值存储）
     ├── RunStore + FeedbackRepository
     ├── ThreadMetaStore（线程元数据）
     ├── RunEventStore（运行事件存储）
     └── RunManager（运行管理器）
  4. 管理员引导检查 → _ensure_admin_user(app)
     ├── 首次启动：提示访问 /setup
     └── 已有管理员：迁移孤立线程
  5. 启动 IM 通道服务 → start_channel_service()
  6. yield —— 服务就绪
```

### 关闭流程

```
lifespan() 关闭阶段：
  1. 停止 IM 通道服务 → stop_channel_service()
     └── 超时限制：5 秒（_SHUTDOWN_HOOK_TIMEOUT_SECONDS）
  2. 关闭持久化引擎 → close_engine()
  3. AsyncExitStack 清理所有资源
```

## 环境变量配置

### Gateway 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `GATEWAY_HOST` | `0.0.0.0` | 绑定主机地址 |
| `GATEWAY_PORT` | `8001` | 绑定端口 |
| `GATEWAY_ENABLE_DOCS` | `true` | 启用 Swagger/ReDoc/OpenAPI 端点 |
| `GATEWAY_CORS_ORIGINS` | （空） | CORS 允许的源（逗号分隔） |

### 认证配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `AUTH_JWT_SECRET` | 自动生成 | JWT 签名密钥（推荐生产环境显式设置） |
| `AUTH_TRUSTED_PROXIES` | （空） | 受信任代理 IP（逗号分隔 CIDR） |
| `DEER_FLOW_CONFIG_PATH` | 自动检测 | 配置文件路径 |

### 通道配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DEER_FLOW_CHANNELS_LANGGRAPH_URL` | `http://localhost:8001/api` | LangGraph API 地址 |
| `DEER_FLOW_CHANNELS_GATEWAY_URL` | `http://localhost:8001` | Gateway API 地址 |

## 关键设计决策

### 1. 认证即默认（Auth by Default）

除明确列出的白名单路径外，所有请求均需认证。白名单包括：
- `/health`、`/docs`、`/redoc`、`/openapi.json`
- `/api/v1/auth/login/local`、`/api/v1/auth/register`
- `/api/v1/auth/logout`、`/api/v1/auth/setup-status`、`/api/v1/auth/initialize`

### 2. 中间件堆叠顺序

FastAPI 中间件按 **添加顺序的逆序** 执行（洋葱模型）。在 `create_app()` 中：
1. 先添加 `AuthMiddleware`（最外层，最后执行）
2. 再添加 `CSRFMiddleware`（中间层）
3. 最后添加 `CORSMiddleware`（最内层，最先执行）

请求进入顺序：Auth → CSRF → CORS → Router

### 3. 进程内通信认证

Channel Worker 运行在同一进程中，通过 `X-DeerFlow-Internal-Token` 头部进行认证。Token 使用 `secrets.token_urlsafe(32)` 在进程启动时生成，不跨进程共享。

### 4. LangGraph 兼容层

`langgraph_auth.py` 为直接使用 LangGraph Server 的场景提供了与 Gateway 相同的 JWT + CSRF 认证逻辑，确保两种部署模式下会话验证一致。

### 5. 配置热更新

`get_app_config()` 缓存解析后的配置，但自动检测文件 mtime 变化并重新加载，无需重启进程即可应用 `config.yaml` 的修改。
