# 06 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **懒初始化 + mtime 缓存** | MCP 连接代价高（进程/网络），首次使用时才建立；mtime 检测外部进程修改 |
| 2 | **MultiServerMCPClient（langchain-mcp-adapters）** | 社区标准实现，避免自造轮子，天然多服务器管理 |
| 3 | **三种传输类型（stdio / SSE / HTTP）** | 覆盖本地子进程、远程推送、远程流式三种部署场景 |
| 4 | **OAuth 支持与自动令牌刷新** | 企业 MCP 服务器需要 Bearer Token，手动管理易出错 |
| 5 | **运行时配置热更新（Gateway API + mtime）** | 无需重启即可增删 MCP 服务器，运维友好 |

---

## 二、逐决策分析

### 决策 1：懒初始化 + mtime 缓存

**问题**：MCP 工具初始化涉及启动子进程（stdio）或建立长连接（SSE/HTTP），成本远高于普通工具注册。何时初始化？如何避免重复初始化？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 应用启动时全量初始化 | 简单直接 | 启动慢；未使用的 MCP 服务器也占资源 |
| 懒初始化 + 固定 TTL 缓存 | 延迟加载 | TTL 过期后无变化也重新初始化；TTL 未过期时配置已变 |
| 懒初始化 + mtime 缓存（当前） | 延迟加载；仅配置变更时重新初始化 | 依赖文件系统 mtime 精度（通常足够） |

**选择懒初始化 + mtime**：`get_cached_mcp_tools()` 首次调用时触发初始化，后续调用直接返回缓存。每次调用都比对 `extensions_config.json` 的 mtime——只有文件被外部修改（Gateway API 进程）才失效。这比固定 TTL 更高效：配置不变时永不触发重连，配置变更时下一次工具请求即刻感知。

**多事件循环兼容**：懒初始化需要运行 async 代码，但调用方可能在 FastAPI 事件循环、LangGraph Studio 独立循环、甚至无循环的上下文中。通过检测 `asyncio.get_event_loop()` 状态分三种路径处理（ThreadPoolExecutor 提交、直接 run_until_complete、asyncio.run），确保所有环境都能正常工作。

---

### 决策 2：MultiServerMCPClient

**问题**：如何管理多个 MCP 服务器的连接和工具发现？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 自行实现多连接管理 | 完全可控 | 工作量大，需处理各传输协议、工具发现、错误隔离 |
| langchain-mcp-adapters（当前） | 社区标准，持续维护 | 依赖外部包版本 |
| 直接使用 MCP SDK | 更底层控制 | 需要适配 LangChain 工具接口 |

**选择 langchain-mcp-adapters**：`MultiServerMCPClient` 已实现多服务器管理、工具发现、名称冲突处理（通过 `tool_name_prefix=True` 添加服务器名前缀如 `filesystem__read_file`）。DeerFlow 只需构建参数字典，不重复造轮子。单服务器失败不影响其他服务器——容错由 `build_servers_config()` 的逐个 try/catch 保证。

---

### 决策 3：三种传输类型

**问题**：MCP 服务器部署形态多样，如何统一接入？

| 传输类型 | 场景 | 机制 | 认证方式 |
|----------|------|------|----------|
| stdio | 本地工具（npx 脚本、Python 命令） | 启动子进程，stdin/stdout 通信 | 通过 env 字段传递 API Key |
| SSE | 远程服务器（Server-Sent Events） | HTTP 长连接，服务器推送 | headers 字段 + OAuth |
| HTTP | 远程服务器（HTTP 流式传输） | HTTP 请求/响应 | headers 字段 + OAuth |

**为什么需要三种**：MCP 生态中，本地工具（如 filesystem、git）以命令行方式提供，适合 stdio；云端服务以 HTTP API 提供，适合 SSE 或 HTTP。三种类型通过 `build_server_params()` 统一映射为 `MultiServerMCPClient` 接受的参数字典，上层代码无需感知差异。

---

### 决策 4：OAuth 支持与自动令牌刷新

**问题**：企业 MCP 服务器要求每次请求携带 OAuth Bearer Token。Token 有过期时间，需要自动刷新。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 手动配置静态 Token | 简单 | Token 过期后服务中断 |
| OAuth 自动管理（当前） | 自动获取/刷新，对上层透明 | 增加 OAuthTokenManager 复杂度 |

**选择自动管理**：`OAuthTokenManager` 使用 double-check locking 模式——快速路径不加锁检查缓存，慢速路径加锁后再次确认（其他协程可能已刷新）。`refresh_skew_seconds`（默认 60 秒）在令牌实际过期前就触发刷新，避免"获取即过期"的竞态。

**拦截器注入**：`build_oauth_tool_interceptor()` 返回符合 langchain-mcp-adapters 签名的拦截器函数，在每次 MCP 工具调用时自动注入 `Authorization` 头。上层代码和 Agent 完全不感知 OAuth——令牌获取、缓存、刷新、注入全部封装在拦截器内。

**两阶段认证**：初始连接阶段（工具发现）需要 Token，通过 `get_initial_oauth_headers()` 在 `MultiServerMCPClient` 创建前注入到 SSE/HTTP 服务器配置的 headers 中；后续调用阶段由拦截器自动处理。

---

### 决策 5：运行时配置热更新

**问题**：运维人员通过 Gateway API 修改 MCP 配置后，Agent 进程需要感知变更并重新加载工具。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 重启 Agent 进程 | 简单可靠 | 服务中断 |
| Gateway API 写文件 + mtime 检测（当前） | 零重启，自动生效 | 跨进程通信依赖文件系统 |

**选择 Gateway API + mtime**：Gateway API 的 `PUT /api/mcp/config` 将新配置写入 `extensions_config.json`（原子操作）。Agent 进程中的 `get_cached_mcp_tools()` 每次调用都检测该文件的 mtime——如果 mtime 大于缓存时记录的值，重置缓存为"未初始化"状态，下次访问触发完整重连。

**为什么不用信号/消息队列**：Gateway 和 Agent 可能运行在同一进程（嵌入式客户端）或不同进程（LangGraph Server）。文件系统是两者都天然共享的介质，零额外基础设施。mtime 精度在所有主流操作系统上均为秒级或亚秒级，足够用。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **按需加载** | 首次 `get_cached_mcp_tools()` 调用时初始化，非启动时 |
| **配置热更新** | mtime 比对 → 缓存失效 → 下次调用自动重连 |
| **企业级认证** | OAuth Token 自动获取/刷新/注入，上层透明 |
| **多服务器容错** | 单服务器失败不影响其他服务器工具加载 |
| **跨事件循环** | 三种 async 上下文（运行中/未运行/不存在）均能处理 |
| **同步兼容** | 异步 MCP 工具包装为同步可调用，兼容 DeerFlowClient |
