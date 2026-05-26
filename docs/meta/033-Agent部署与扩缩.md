# Agent 部署与扩缩

**问题**: Agent 从本地跑到生产环境，需要解决 GPU 资源调度、长连接管理、热更新、灰度发布等运维问题。直接把 Agent 丢到 Kubernetes 不一定能跑好。

---

## 问题 1：Agent 部署和传统 Web 部署有什么区别？

| 维度 | 传统 Web | Agent 系统 |
|------|---------|-----------|
| 请求模式 | 短连接（ms 级） | 长连接 SSE（分钟级） |
| 资源消耗 | CPU + 内存 | GPU（推理）+ 内存（上下文） |
| 并发瓶颈 | 数据库连接 | LLM API 限流 |
| 状态管理 | 无状态 | 有状态（对话上下文） |
| 扩缩依据 | QPS | 并发 Agent 数 |

不能直接套用传统 Web 的部署经验。

---

## 问题 2：DeerFlow 的部署架构是什么？

```
┌──────────────────────────────────────────┐
│                Nginx / CDN                │
├──────────────┬───────────────────────────┤
│  Frontend    │        Gateway (FastAPI)    │
│  (Next.js)   │  ├── 认证中间件             │
│  静态文件     │  ├── CSRF 中间件            │
│              │  ├── SSE 长连接             │
│              │  └── 路由分发               │
├──────────────┼───────────────────────────┤
│              │     Agent Runtime          │
│              │  ├── RunManager            │
│              │  ├── 20 个中间件            │
│              │  ├── 子 Agent 执行器        │
│              │  └── StreamBridge          │
├──────────────┼───────────────────────────┤
│              │     External Dependencies  │
│              │  ├── LLM API (OpenAI/Claude)│
│              │  ├── MCP Servers           │
│              │  ├── 数据库 (SQLite/PG)     │
│              │  └── 追踪 (LangSmith)      │
└──────────────┴───────────────────────────┘
```

Frontend 和 Gateway 可以分开部署，也可以打包在一起。

---

## 问题 3：SSE 长连接怎么管理？

SSE 连接持续整个 Agent 执行过程（可能数分钟），挑战：

| 挑战 | 解决方案 |
|------|---------|
| 连接超时 | Nginx 配置 `proxy_read_timeout 600s` |
| 反向代理缓冲 | Nginx 关闭缓冲: `proxy_buffering off` |
| 断线重连 | 前端 EventSource 自动重连 + Last-Event-ID |
| 资源泄漏 | StreamBridge 限制 256 事件 + GC 清理 |

```nginx
# Nginx SSE 配置
location /api/ {
    proxy_pass http://gateway:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 600s;
    proxy_cache off;
}
```

---

## 问题 4：LLM API 限流怎么应对？

LLM Provider 都有 RPM（每分钟请求数）和 TPM（每分钟 token 数）限制：

```
应对策略:

1. 限流重试
   RateLimitError → 指数退避 → 1s, 2s, 4s

2. 多 Key 轮换
   api_keys: ["sk-xxx1", "sk-xxx2", "sk-xxx3"]
   → 轮流使用，RPM × 3

3. 多 Provider 降级
   claude → 限流 → 切换到 openai → 继续服务

4. 请求排队
   超过限流 → 排队等待 → 不直接拒绝
```

```yaml
# 多 Key 配置
models:
  providers:
    claude:
      api_key: "${ANTHROPIC_API_KEY}"  # 支持逗号分隔多 Key
      fallback_provider: "openai"       # 降级到 OpenAI
```

---

## 问题 5：数据库怎么选？

| 数据库 | 适用场景 | 优缺点 |
|--------|---------|-------|
| SQLite | 单机/开发 | 零配置，但不支持并发写入 |
| PostgreSQL | 生产/多实例 | 支持并发，需要额外部署 |

```yaml
persistence:
  provider: "postgresql"  # 或 "sqlite"
  connection_string: "${DATABASE_URL}"
```

DeerFlow 的持久化层通过接口抽象，切换数据库不需要改代码。

---

## 问题 6：如何做热更新？

| 组件 | 热更新方式 | 停机时间 |
|------|-----------|---------|
| 技能 | 修改 skills/ 目录文件 | 零（下次请求生效） |
| MCP 工具 | 修改 extensions_config.json | 零（缓存检测到变更） |
| Agent 配置 | 修改 config.yaml | 需要重启 |
| Agent 代码 | Git pull + 重启 | 短暂中断 |

技能和 MCP 可以热加载——修改文件后下一个请求自动生效。

---

## 问题 7：如何做灰度发布？

```
策略 1: 配置分流
    ├── 10% 用户 → 新版本 Agent
    └── 90% 用户 → 旧版本 Agent

策略 2: 功能开关
    features:
      new_reasoning: true  # 新推理策略
      new_memory: false    # 新记忆系统（暂不启用）

策略 3: 多实例部署
    ├── 实例 A（v1）← 生产流量
    └── 实例 B（v2）← 内测用户
```

DeerFlow 的 `RuntimeFeatures` 数据类天然支持功能开关：

```yaml
features:
  summarization: true
  loop_detection: true
  new_feature: false  # 灰度控制
```

---

## 问题 8：如何监控生产健康？

| 监控维度 | 指标 | 告警阈值 |
|---------|------|---------|
| 可用性 | API 响应率 | < 99.5% |
| 性能 | P95 首 token 延迟 | > 5s |
| 质量 | 工具错误率 | > 20% |
| 成本 | 平均 Token/Run | > 50k |
| 资源 | 活跃 SSE 连接数 | > 80% 上限 |
| LLM | API 失败率 | > 5% |

```
监控链路:
Agent 执行 → RunJournal → 数据库
    │
    ▼
Prometheus/Grafana Dashboard
    │
    ▼
告警规则 → PagerDuty/飞书
```

---

## 问题 9：如何水平扩展？

```
单实例瓶颈:
    ├── CPU: Agent 本身不吃 CPU（推理在 LLM API 侧）
    ├── 内存: 对话上下文 + 记忆缓存
    ├── 连接: SSE 长连接数上限
    └── LLM API: Provider 限流

扩展方案:
┌──────────┐     ┌──────────┐     ┌──────────┐
│ 实例 1   │     │ 实例 2   │     │ 实例 3   │
│ Gateway  │     │ Gateway  │     │ Gateway  │
│ + Agent  │     │ + Agent  │     │ + Agent  │
└────┬─────┘     └────┬─────┘     └────┬─────┘
     │                │                │
     └────────────────┼────────────────┘
                      │
               ┌──────▼──────┐
               │  PostgreSQL  │  ← 共享数据库
               │  (状态/事件)  │
               └─────────────┘
```

关键：多实例共享数据库（检查点、事件流），但每个实例独立管理内存中的锁和 StreamBridge。

---

## 问题 10：Docker 部署怎么做？

```yaml
# docker-compose.yml
services:
  frontend:
    build: ./frontend
    ports: ["3000:3000"]

  gateway:
    build: ./backend
    ports: ["8000:8000"]
    env_file: .env
    volumes:
      - ./skills:/app/skills          # 技能目录
      - ./data:/app/.deer-flow         # 数据目录
    depends_on:
      - db

  db:
    image: postgres:16
    environment:
      POSTGRES_DB: deerflow
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

```bash
# 启动
docker compose up -d

# 更新技能（热加载，无需重启）
cp new-skill/ skills/
# 下次请求自动加载

# 更新代码（需要重启）
git pull
docker compose build gateway
docker compose up -d gateway
```

---

## 数据流概览

```
用户请求
    │
    ▼ 负载均衡（Nginx）
    │
    ▼ Gateway 实例（无状态，可水平扩展）
    │
    ▼ Agent Runtime（有状态，SSE 长连接）
    │   ├── 检查点 → PostgreSQL（共享）
    │   ├── 事件流 → PostgreSQL（共享）
    │   └── 追踪   → LangSmith（外部）
    │
    ▼ LLM API（外部，限流控制）
    │
    ▼ SSE 响应 → 用户
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| Docker 配置 | `docker/` 目录 |
| Gateway 入口 | `backend/app/gateway/app.py` |
| 配置文件 | `config.example.yaml` |
| 运行时管理 | `backend/packages/harness/deerflow/runtime/` |

## 深入阅读

- [请求全链路](015-请求全链路.md) — SSE 长连接处理
- [事件流与持久化](017-事件流与持久化.md) — 共享数据库设计
- [模型工厂](016-模型工厂与多Provider.md) — 多 Provider 降级
- [可观测性](027-Agent可观测性与调试.md) — 生产监控
