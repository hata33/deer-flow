---
name: sandbox-system
description: 沙箱两种模式（Local/Docker）的执行环境、路径翻译、bash 权限控制
---

## 两种 Provider

| Provider | 类路径 | 执行环境 | 隔离方式 |
|---|---|---|---|
| LocalSandboxProvider | `deerflow.sandbox.local:LocalSandboxProvider` | Windows 本地进程 | 虚拟路径映射 + 权限校验 |
| AioSandboxProvider | `deerflow.community.aio_sandbox:AioSandboxProvider` | Linux Docker 容器 | 容器完整隔离 |

配置位置：`config.yaml` → `sandbox.use`

## 虚拟路径翻译

Agent 看到的虚拟路径会由沙箱层翻译为物理路径：

| 虚拟路径 | 本地模式物理路径 |
|---|---|
| `/mnt/user-data/workspace/` | `.deer-flow/users/{uid}/threads/{tid}/user-data/workspace/` |
| `/mnt/user-data/outputs/` | `.deer-flow/users/{uid}/threads/{tid}/user-data/outputs/` |
| `/mnt/skills/` | `skills/` 目录 |

LocalSandbox 在 Windows 上会做路径分隔符转换，并自动检测 shell（pwsh → powershell → cmd → Git Bash）。

## bash 执行权限

**核心开关**：`config.yaml` → `sandbox.allow_host_bash`

| 配置值 | LocalSandboxProvider | AioSandboxProvider |
|---|---|---|
| `false`（默认） | bash 完全禁止，返回错误 | 始终允许（容器内） |
| `true` | 允许在宿主机执行任意命令 | 始终允许（容器内） |

**安全机制层级**（见 `docs/core/sandbox/00-overview.md` 安全机制部分）：
1. 路径验证 — 只能访问虚拟路径白名单
2. Bash 命令路径验证 — 检查命令中的绝对路径
3. 路径遍历防护 — 拦截 `..`
4. 只读挂载强制
5. 输出屏蔽 — 物理路径不会泄露给 Agent
6. 命令安全审计 — SandboxAuditMiddleware
7. 宿主 Bash 保护 — `is_host_bash_allowed()`

## Agent 安装 Python 库的行为

- bash 工具文档建议 agent 使用线程级虚拟环境 `/mnt/user-data/workspace/.venv`
- 但这需要 `allow_host_bash: true` 才能执行
- Docker 模式下随意安装，不影响宿主机
- 默认配置下 agent 无法执行任何 bash 命令

## 关键代码位置

- 沙箱工具定义：`backend/packages/harness/deerflow/sandbox/tools.py`
- 本地沙箱实现：`backend/packages/harness/deerflow/sandbox/local/local_sandbox.py`
- 安全判断：`backend/packages/harness/deerflow/sandbox/security.py`
- 配置模型：`backend/packages/harness/deerflow/config/sandbox_config.py`
- 审计中间件：`backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py`

## Why: 沙箱是 deer-flow 的核心安全边界，理解其执行模式对排查 skill 运行问题至关重要。
## How to apply: 当 skill 执行环境异常时，首先检查 sandbox.use 和 allow_host_bash 配置。
