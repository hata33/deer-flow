# 沙箱系统全局概览

沙箱系统为 DeerFlow Agent 工具提供代码执行隔离环境。Agent 通过统一的虚拟路径接口操作文件系统和执行命令，底层实现由 Provider 负责将虚拟路径映射到宿主机或容器内的物理路径。这种抽象确保了 Agent 工具代码无需关心执行环境的差异——无论是本地文件系统还是 Docker 容器，工具看到的路径和 API 行为完全一致。

## 系统定位

沙箱位于 Agent 工具层与操作系统之间，是所有文件操作和命令执行的唯一通道：

```
Agent LLM
   │
   ▼
Sandbox Tools (bash / ls / read_file / write_file / str_replace / glob / grep)
   │
   ▼
Sandbox 抽象接口 (Sandbox ABC)
   │
   ├── LocalSandbox  ← 本地文件系统执行
   └── AioSandbox    ← Docker 容器隔离执行
```

## 虚拟路径系统

Agent 视角下只看到统一的虚拟路径前缀，不感知底层物理路径布局：

| 虚拟路径 | 物理路径（本地模式） | 用途 |
|----------|----------------------|------|
| `/mnt/user-data/workspace/` | `.deer-flow/users/{user_id}/threads/{thread_id}/user-data/workspace/` | Agent 工作空间（读写） |
| `/mnt/user-data/uploads/` | `.deer-flow/users/{user_id}/threads/{thread_id}/user-data/uploads/` | 用户上传文件（读写） |
| `/mnt/user-data/outputs/` | `.deer-flow/users/{user_id}/threads/{thread_id}/user-data/outputs/` | Agent 输出产物（读写） |
| `/mnt/user-data/` | `.deer-flow/users/{user_id}/threads/{thread_id}/user-data/` | 上述三目录的聚合父目录 |
| `/mnt/skills/` | `skills/` 目录 | 技能文件（只读） |
| `/mnt/acp-workspace/` | `.deer-flow/users/{user_id}/threads/{thread_id}/acp-workspace/` | ACP 代理工作空间（只读） |
| 自定义挂载 | config.yaml sandbox.mounts 配置 | 可配置读写或只读 |

AioSandboxProvider 通过 Docker 卷挂载将这些目录映射到容器内的相同虚拟路径，因此两种实现的路径契约完全一致。

## Provider 模式

沙箱生命周期通过 `SandboxProvider` 抽象基类管理：

```
SandboxProvider (ABC)
├── acquire(thread_id) → sandbox_id    获取沙箱实例
├── get(sandbox_id) → Sandbox | None   查询已有实例
├── release(sandbox_id)                释放沙箱实例
└── reset()                            重置所有缓存状态
```

两种实现：

| Provider | 类路径 | 隔离方式 | 适用场景 |
|----------|--------|----------|----------|
| `LocalSandboxProvider` | `deerflow.sandbox.local:LocalSandboxProvider` | 虚拟路径映射 + 权限校验 | 本地开发、可信环境 |
| `AioSandboxProvider` | `deerflow.community.aio_sandbox:AioSandboxProvider` | Docker 容器完整隔离 | 生产环境、不可信代码 |

## 路径映射机制

`PathMapping` 数据结构实现容器路径与本地路径的双向翻译：

```python
@dataclass(frozen=True)
class PathMapping:
    container_path: str   # 虚拟路径前缀，如 /mnt/user-data/workspace
    local_path: str       # 物理路径前缀，如 /home/user/.deer-flow/.../workspace
    read_only: bool       # 是否只读挂载
```

**正向解析**：容器路径 → 本地路径（命令执行前、文件写入内容中的路径替换）
**反向解析**：本地路径 → 容器路径（命令输出、目录列表中的路径屏蔽）

## 安全机制

沙箱系统的安全由多层防线组成：

1. **路径验证** — `validate_local_tool_path()` 限制只能访问虚拟路径白名单
2. **Bash 命令验证** — `validate_local_bash_command_paths()` 检查命令中的绝对路径合法性
3. **路径遍历防护** — `_reject_path_traversal()` 拦截 `..` 段
4. **只读挂载强制** — `_is_read_only_path()` 阻止对只读目录的写入操作
5. **输出屏蔽** — `mask_local_paths_in_output()` 防止宿主机路径泄露到 Agent 输出
6. **命令安全审计** — `SandboxAuditMiddleware` 检测高风险/中风险命令并记录审计日志
7. **宿主 Bash 保护** — `is_host_bash_allowed()` 默认禁止 LocalSandbox 直接执行宿主 bash

## 输出屏蔽

Agent 看到的所有输出（bash 执行结果、文件内容、目录列表）中的宿主机物理路径都会被反向解析回虚拟路径。这确保：

- Agent 不会感知到宿主机目录布局
- 宿主机绝对路径不会出现在对话上下文中
- 错误消息中的物理路径也会被清理

## 模块结构

```
sandbox/
├── __init__.py                  ← 包入口，导出 Sandbox / SandboxProvider
├── sandbox.py                   ← Sandbox ABC 抽象基类
├── sandbox_provider.py          ← SandboxProvider ABC + 全局单例管理
├── exceptions.py                ← SandboxError 异常层次结构
├── security.py                  ← 安全工具函数（Provider 检测、权限判断）
├── tools.py                     ← Agent 工具定义（bash/ls/read_file/write_file/str_replace/glob/grep）
├── middleware.py                 ← SandboxMiddleware 沙箱生命周期中间件
├── search.py                    ← glob/grep 搜索引擎 + GrepMatch 数据结构
├── file_operation_lock.py       ← 文件操作锁（per sandbox_id + path）
└── local/                       ← 本地文件系统实现
    ├── __init__.py              ← 导出 LocalSandboxProvider
    ├── local_sandbox.py         ← LocalSandbox 实现 + PathMapping 数据类
    ├── local_sandbox_provider.py← LocalSandboxProvider（LRU 缓存 + 线程安全）
    └── list_dir.py              ← 目录递归列表（带忽略规则）
```

关联模块（不在 sandbox/ 目录内）：

- `config/sandbox_config.py` — `SandboxConfig` + `VolumeMountConfig` 配置模型
- `config/paths.py` — `VIRTUAL_PATH_PREFIX` 常量（`/mnt/user-data`）+ 路径工具函数
- `agents/middlewares/sandbox_audit_middleware.py` — Bash 命令安全审计中间件
- `community/aio_sandbox/` — Docker 容器沙箱实现（AioSandboxProvider）
