# Agent 安全防护

**问题**: Agent 能执行 bash 命令、读写文件、访问网络，如果被恶意利用（prompt 注入、路径穿越、工具滥用），后果比普通 Web 漏洞严重得多。

---

## 问题 1：Agent 面临哪些安全威胁？

| 威胁 | 攻击方式 | 后果 |
|------|---------|------|
| Prompt 注入 | 用户输入包含恶意指令 | Agent 执行非预期操作 |
| 路径穿越 | 工具参数包含 `../../etc/passwd` | 读取系统敏感文件 |
| 工具滥用 | Agent 调用危险工具组合 | 数据删除、权限提升 |
| 资源耗尽 | 触发死循环或大量子 Agent | API 配额耗尽、服务不可用 |
| 数据泄露 | Agent 将敏感信息发送到外部 | 隐私泄露 |
| 供应链攻击 | 恶意 SKILL.md 或 MCP 服务器 | Agent 行为被篡改 |

---

## 问题 2：Prompt 注入怎么防护？

多层防御，没有银弹：

**第一层：工具权限控制**

```yaml
# 护栏限制可用工具
guardrails:
  provider: deerflow.guardrails.builtin:AllowlistProvider
  config:
    allowed_tools: ["bash", "read_file", "write_file"]
    denied_tools: ["curl", "wget"]  # 禁止外发请求
```

即使被注入，Agent 也无法调用 `curl` 发送数据。

**第二层：沙箱路径限制**

```python
# Agent 只能访问虚拟路径
/mnt/user-data/workspace  →  用户工作区
/mnt/skills               →  只读技能目录

# 路径穿越被拒绝
/mnt/user-data/../../etc/passwd → _reject_path_traversal() → 拒绝
```

**第三层：命令安全审计**

```python
# bash 命令风险评估
if verdict == "block":
    # rm -rf /, dd, mkfs 等 → 直接拒绝
    return block_message
if verdict == "warn":
    # curl 外部 URL 等 → 允许但记录
    log.warning("suspicious command: %s", command)
```

---

## 问题 3：路径穿越攻击怎么防？

`security.py` 中的三层校验：

```python
def validate_local_tool_path(path, thread_data, *, read_only=False):
    # 第 1 层：路径规范化 + 穿越 detecting
    _reject_path_traversal(path)
    # 拒绝: ..、//、符号链接逃逸、null byte

    # 第 2 层：路径族匹配
    if path.startswith("/mnt/user-data/"):
        return  # 用户数据区，允许
    elif path.startswith("/mnt/skills/"):
        if read_only:
            return  # 只读访问技能区
        raise PermissionError("Skills directory is read-only")

    # 第 3 层：默认拒绝
    raise PermissionError(f"Path not in allowed families: {path}")
```

**默认拒绝**策略：不在白名单路径族中的路径一律拒绝。

---

## 问题 4：如何防止工具滥用？

三层过滤的交集：

```
所有已注册工具
    │
    ▼ 护栏过滤（全局策略）
拒绝 denied_tools 中的工具
    │
    ▼ 技能工具策略（技能级过滤）
只保留技能 allowed-tools 中的工具
    │
    ▼ 沙箱限制（执行级）
即使工具允许，操作范围受沙箱约束
    │
    ▼ 最终可用工具列表
```

一个工具必须同时通过三层才能使用。

---

## 问题 5：恶意技能/ MCP 服务器怎么防？

**技能安全扫描**:

```python
# skills/security_scanner.py
# 加载时检查 SKILL.md
- 检测恶意指令（如试图修改沙箱配置）
- 检测危险工具声明
- 验证元数据格式
```

**MCP 连接隔离**:

```
MCP 服务器是独立进程（非嵌入）
    │
    ▼ 通过 stdio/SSE 通信
    │
    ▼ 工具调用受限
    ├── 护栏控制哪些 MCP 工具可用
    └── 沙箱限制文件操作范围
```

即使恶意 MCP 服务器想执行危险操作，也要通过护栏和沙箱的检查。

---

## 问题 6：资源耗尽攻击怎么防？

四层限制：

| 资源 | 限制 | 机制 |
|------|------|------|
| LLM 调用轮数 | recursion_limit | LangGraph 内置 |
| 工具重复调用 | 3-5 次强停 | 循环检测中间件 |
| 子 Agent 并发 | 最多 3 个 | subagent_limit_middleware |
| 子 Agent 运行时间 | 15 分钟超时 | executor 超时机制 |
| bash 命令执行 | 10 分钟超时 | subprocess.run timeout |
| 上下文大小 | 10 万 token 触发压缩 | summarization_middleware |
| 记忆大小 | 100 条 facts 上限 | memory 配置 |

即使恶意用户试图耗尽资源，每层都有硬上限。

---

## 问题 7：敏感数据怎么防泄露？

**错误消息截断**:

```python
# 工具错误消息最多 500 字符
detail = str(exc).strip() or exc.__class__.__name__
if len(detail) > 500:
    detail = detail[:497] + "..."
```

防止异常堆栈中包含的文件路径、数据库连接串等泄露给 Agent。

**JWT Secret 管理**:

```
优先级: 环境变量 > .jwt_secret 文件 > 自动生成
生产必须用环境变量 → 不进入代码仓库
```

**CSRF 保护**:

```
Cookie: csrf_token=<random>
Header: X-CSRF-Token: <same_random>
两者必须一致 → 防止跨站请求伪造
```

---

## 问题 8：审计日志记录什么？

`sandbox_audit_middleware` 记录所有敏感操作：

```
[audit] user=a thread=1 tool=bash command="cat /etc/hosts" verdict=warn
[audit] user=a thread=1 tool=write_file path=/mnt/user-data/workspace/config.yaml
[audit] user=b thread=3 tool=bash command="rm -rf /tmp/test" verdict=block
```

日志包含：用户、线程、工具、参数/路径、安全评估结果。用于事后回溯。

---

## 问题 9：多租户隔离怎么保证？

物理隔离 + 逻辑隔离：

```
物理隔离（文件系统）:
  用户 A: .deer-flow/users/a/threads/1/
  用户 B: .deer-flow/users/b/threads/2/
  → 路径映射保证 A 永远无法访问 B 的文件

逻辑隔离（运行时）:
  每个 Thread 独立 asyncio.Lock
  → A 的 Run 不会影响 B 的执行

记忆隔离:
  每个 Agent 有独立的 memory.json
  → A 的偏好不会泄漏给 B
```

---

## 问题 10：安全防护的完整架构？

```
请求入口
    │
    ▼ 认证层
    ├── JWT 验证 + 版本号校验
    ├── CSRF 双重提交
    └── 内部认证（服务间）
    │
    ▼ 工具调用层
    ├── 沙箱审计（命令风险评估）
    ├── 延迟工具过滤
    ├── 护栏（工具权限策略）
    └── 工具错误处理（截断敏感信息）
    │
    ▼ 执行层
    ├── 沙箱路径映射（路径穿越防护）
    ├── 资源限制（循环检测 + 并发控制 + 超时）
    └── 审计日志（操作记录）
    │
    ▼ 数据层
    ├── 多租户隔离（路径 + 锁 + 记忆）
    └── 安全存储（JWT Secret 环境变量）
```

---

## 数据流概览

```
用户输入（可能包含恶意指令）
    │
    ▼ 认证检查
    │
    ▼ Agent 解析意图
    │
    ▼ 选择工具调用
    │
    ▼ 沙箱审计 → 高危? → block
    │
    ▼ 护栏检查 → 不允许? → denied
    │
    ▼ 沙箱执行 → 路径越界? → PermissionError
    │
    ▼ 错误处理 → 截断消息 → 返回 Agent
    │
    ▼ 审计日志 → 记录操作
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 安全校验 | `backend/packages/harness/deerflow/sandbox/security.py` |
| 沙箱审计 | `backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py` |
| 护栏中间件 | `backend/packages/harness/deerflow/guardrails/middleware.py` |
| 内置护栏 | `backend/packages/harness/deerflow/guardrails/builtin.py` |
| 技能安全扫描 | `backend/packages/harness/deerflow/skills/security_scanner.py` |
| 循环检测 | `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` |
| 认证中间件 | `backend/packages/harness/deerflow/auth/middleware.py` |
| CSRF | `backend/packages/harness/deerflow/auth/csrf_middleware.py` |

## 深入阅读

- [沙箱安全](005-沙箱安全.md) — 文件系统隔离
- [护栏系统](008-护栏系统.md) — 工具权限控制
- [认证与授权](007-认证与授权.md) — 用户认证
- [循环检测机制](013-循环检测机制.md) — 资源耗尽防护
- [工具调用失败处理](022-工具调用失败处理.md) — 错误信息脱敏
