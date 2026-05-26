# Gateway 设计决策

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

## 核心决策清单

| # | 决策 | 解决的问题 | 权衡 |
|---|------|-----------|------|
| D1 | FastAPI + 嵌入式 LangGraph 运行时（单进程） | 部署复杂度、进程间通信开销 | 单进程扩展上限、耦合度高 |
| D2 | 同源 CORS 默认 + opt-in CORS_ORIGINS | 默认安全 vs 浏览器跨域需求 | 配置不当可能导致请求被拒 |
| D3 | CSRF Double Submit Cookie 对齐 CORS 白名单 | CORS 与 CSRF 来源校验不一致 | 认证端点需单独处理首次无 Token 场景 |
| D4 | RunManager.get() 异步 | 持久化 RunStore 的 I/O 水合 | 调用方必须 await，增加异步复杂度 |
| D5 | SSE 心跳间隔 15 秒 | 代理/负载均衡器超时断连 | 增加少量空闲带宽 |
| D6 | 活跃内容类型强制下载 | HTML/SVG 在同源上下文中的 XSS | 用户无法内联预览 HTML 产物 |

---

## D1: FastAPI + 嵌入式 LangGraph 运行时（单进程）

**动机**: DeerFlow 的典型部署是单机全栈（Gateway + Frontend + Nginx）。将 LangGraph 运行时嵌入 Gateway 进程消除了独立 LangGraph Server 的运维负担——无需管理第二个服务进程、无需配置进程间认证、无需维护 gRPC/HTTP 内部通信。

**解决的问题**:
- 独立 LangGraph Server 需要额外的进程编排和健康检查
- 进程间通信引入网络延迟和序列化开销
- 两个服务各自持有检查点器和存储实例，浪费内存

**权衡**:
- **耦合**: 运行时与 Gateway 共享进程空间，无法独立扩缩容。对于需要水平扩展 Agent 运行能力的场景（如大规模并发），需要引入外部 LangGraph Platform
- **单进程上限**: 所有 Agent 执行共享同一 uvicorn Worker 的线程池，CPU 密集型推理任务可能互相影响
- **可接受的原因**: DeerFlow 定位为个人/团队级 AI 超级 Agent，单机部署足够覆盖目标场景

**关键实现**: `deps.py` 的 `langgraph_runtime()` 使用 `AsyncExitStack` 管理所有运行时单例的生命周期，确保启动顺序正确（StreamBridge -> 持久化引擎 -> Checkpointer -> Store -> 仓库 -> RunManager）。

---

## D2: 同源 CORS 默认 + opt-in CORS_ORIGINS

**动机**: 生产环境中 Nginx 统一入口（port 2026）将前端和 API 反向代理到同一域名下，浏览器请求天然同源，不需要 CORS。仅在开发端口直连或分域部署时才需要跨域支持。

**解决的问题**:
- 默认允许所有 Origin（`allow_origins=["*"]`）会导致 Cookie 凭证被浏览器静默丢弃（`credentials=True` 与 `*` 互斥）
- 过度宽松的 CORS 策略增加 CSRF 攻击面

**权衡**:
- 开发者使用 `localhost:3000` 直连 `localhost:8001` 时需要额外配置 `GATEWAY_CORS_ORIGINS=http://localhost:3000`
- 分源部署（如前端在 `app.example.com`，API 在 `api.example.com`）需要显式列出每个 Origin

**关键实现**: `create_app()` 读取 `get_configured_cors_origins()` 返回的白名单，仅在非空时注册 `CORSMiddleware`。白名单通过 `_normalize_origin()` 归一化（去除路径、查询、凭据），只保留 `scheme://host[:port]`。

---

## D3: CSRF Double Submit Cookie 对齐 CORS

**动机**: CORS 和 CSRF 是两个互补的浏览器安全机制。如果两者使用不同的来源白名单，攻击者可能找到一个在 CORS 中被允许但在 CSRF 中不被检查的 Origin（或反之），从而绕过防护。

**解决的问题**: 登录 CSRF（攻击者构造跨站表单让受害者登录攻击者账号）和会话固定攻击。

**权衡**:
- 认证端点（`/login`、`/register`）首次请求时没有 CSRF Token，需要单独处理：免于 Double Submit 检查，但仍做 Origin 校验
- 非 OAuth 场景下（无 `Origin` 头的 API 客户端如 curl/移动端）直接放行，依赖其他层的认证（JWT/内部令牌）

**关键实现**: `CSRFMiddleware` 分两层——认证端点检查 `is_allowed_auth_origin()`（CORS 白名单 OR 同源），非认证端点做 Double Submit Cookie 比较（`secrets.compare_digest` 防时序攻击）。两层共享 `_configured_cors_origins()` 数据源。

---

## D4: RunManager.get() 异步

**动机**: `RunManager` 的 `get()` 方法需要从持久化 `RunStore`（PostgreSQL/SQLite）水合历史运行记录。数据库 I/O 是异步操作（`asyncpg` / `aiosqlite`），因此 `get()` 必须是 `async`。

**解决的问题**:
- 重启后恢复运行历史（页面刷新显示之前的运行列表）
- 跨 Worker 查看已持久化的运行记录（store-only 记录可读但不可操作）

**权衡**:
- 所有调用方（路由、管理器）必须 `await run_mgr.get(run_id)`，增加了异步传染性
- 内存中的活跃记录和持久化的历史记录可能不一致——代码用 `store_only` 标记区分：store-only 记录没有 `task` 和流控制状态，取消操作返回 409

**关键实现**: `get()` 先查内存字典（O(1)），未命中再查 `RunStore`。内存记录优先级更高——同一个 `run_id` 的活跃任务状态不会被持久化记录覆盖。

---

## D5: SSE 心跳间隔 15 秒

**动机**: SSE 连接是长连接，中间的代理（Nginx、CDN、负载均衡器）通常有空闲超时（通常 60 秒）。如果 Agent 思考时间较长（复杂推理可达数分钟），连接可能被中间层主动断开，导致客户端收到异常中断。

**解决的问题**: 长时间无事件的 SSE 连接被代理/负载均衡器超时关闭。

**权衡**:
- 15 秒间隔在 60 秒超时下有足够余量（3-4 个心跳周期），但也意味着即使在无 Agent 活动的空闲连接上也有持续的微小网络开销
- 更短的间隔（如 5 秒）更安全但增加带宽；更长的间隔（如 30 秒）在极端思考时间下可能不够

**关键实现**: `StreamBridge` 定期注入 `HEARTBEAT_SENTINEL`，`sse_consumer()` 将其转换为 SSE 注释帧 `": heartbeat\n\n"`。注释帧不会触发浏览器 `EventSource.onmessage`，但保持 TCP 连接活跃。

---

## D6: 活跃内容类型强制下载

**动机**: Gateway 在同源上下文中提供 Artifact 文件服务（`/api/threads/{id}/artifacts/{path}`）。AI 生成的 HTML、XHTML、SVG 文件可能包含 `<script>` 标签或事件处理器。如果浏览器在同源上下文中直接渲染这些文件，脚本可以访问用户的会话 Cookie、读取页面数据、发起 API 请求。

**解决的问题**: AI 生成的 HTML/SVG 产物在同源上下文中的存储型 XSS。

**权衡**:
- 用户无法在浏览器中直接预览 HTML/SVG 产物（必须下载后打开或使用专门的预览工具）
- 这是有意的安全权衡——AI 生成的代码不可信，不应在应用安全上下文中执行
- 其他类型（图片、PDF、文本）仍可内联显示

**关键实现**: `ACTIVE_CONTENT_MIME_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}`，`get_artifact()` 端点对这些类型始终设置 `Content-Disposition: attachment`，无论客户端是否请求下载。
