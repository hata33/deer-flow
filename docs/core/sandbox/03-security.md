# 安全机制

沙箱系统的安全由多层防线组成，从路径验证、命令审计到宿主机保护，形成纵深防御体系。安全代码主要分布在 `sandbox/tools.py`（路径验证、命令验证）、`sandbox/security.py`（Provider 级安全策略）和 `agents/middlewares/sandbox_audit_middleware.py`（命令安全审计中间件）中。

## 路径验证：validate_local_tool_path

`validate_local_tool_path(path, thread_data, *, read_only=False)` 是所有文件工具的第一道安全门。它检查虚拟路径是否属于允许访问的路径族，并在违规时抛出 `PermissionError`。

### 允许的虚拟路径族

| 虚拟路径前缀 | 读写权限 | 条件 |
|---------------|----------|------|
| `/mnt/user-data/*` | 读写 | 始终允许 |
| `/mnt/skills/*` | 只读 | 仅当 `read_only=True` |
| `/mnt/acp-workspace/*` | 只读 | 仅当 `read_only=True` |
| 自定义挂载路径 | 取决于配置 | 遵循每个挂载的 `read_only` 标志 |

**验证顺序**：

1. 检查 `thread_data` 是否存在（缺失则抛出 `SandboxRuntimeError`）
2. `_reject_path_traversal(path)` — 拒绝包含 `..` 段的路径
3. 按顺序检查 skills / acp-workspace / user-data / 自定义挂载
4. 未匹配任何允许族时抛出 `PermissionError`

### 路径遍历防护

`_reject_path_traversal(path)` 将路径中的 `\` 统一为 `/` 后按段检查，任何 `..` 段立即触发 `PermissionError`：

```python
def _reject_path_traversal(path: str) -> None:
    normalised = path.replace("\\", "/")
    for segment in normalised.split("/"):
        if segment == "..":
            raise PermissionError("Access denied: path traversal detected")
```

### 解析后验证

`_validate_resolved_user_data_path(resolved, thread_data)` 在虚拟路径被替换为物理路径后，额外验证解析结果仍在允许的根目录内：

```python
allowed_roots = [
    Path(thread_data["workspace_path"]).resolve(),
    Path(thread_data["uploads_path"]).resolve(),
    Path(thread_data["outputs_path"]).resolve(),
]
for root in allowed_roots:
    try:
        resolved.relative_to(root)
        return  # 在允许范围内
    except ValueError:
        continue
raise PermissionError("Access denied: path traversal detected")
```

这是防御链中最关键的检查——即使虚拟路径通过了白名单校验，解析后的物理路径仍需被限定在预期范围内。

## Bash 命令验证：validate_local_bash_command_paths

`validate_local_bash_command_paths(command, thread_data)` 对 bash 工具的命令字符串进行多维度安全扫描。此验证仅为 LocalSandboxProvider + `allow_host_bash: true` 的最佳努力防护，不构成安全的沙箱边界。

### 验证层次

```
① file:// URL 拦截
② Shell token 解析与审计
③ 绝对路径正则扫描
```

### file:// URL 拦截

```python
_FILE_URL_PATTERN = re.compile(r"\bfile://\S+", re.IGNORECASE)
```

`file://` URL 可绕过绝对路径正则但允许本地文件外泄，因此被直接拦截。

### Shell token 解析

`_validate_local_bash_shell_tokens(command, allowed_paths)` 对命令进行深度结构化分析：

1. **命令替换检测** — `re.search(r"\$\([^)]*\b(?:cd|pushd)\b", command)` 拦截 `$(cd ...)` 形式的目录切换
2. **Token 化** — `_split_shell_tokens(command)` 使用 `shlex.shlex` 将命令拆分为 token
3. **遍历段检测** — 对每个 token 检查 `..` 段（排除 URL token）
4. **命令结构分析** — 遍历 token 流，识别：
   - Shell 分隔符：`;` `&&` `||` `|` `&` `(` `)`
   - 重定向操作符：`<` `>` `>>` `<<` 等
   - 变量赋值：`NAME=value` 模式
   - 命令前缀关键字：`if` `for` `while` `case` 等
   - 命令包装器：`command` `builtin`

### cd/pushd 目标验证

`_validate_local_bash_cwd_target(command_name, target, allowed_paths)` 验证 `cd` 和 `pushd` 的目标路径：

- `target is None` 或 `"-"` → 拒绝（不安全的目录切换）
- 以 `$` 或反引号开头 → 拒绝（变量/命令替换）
- 以 `~` 开头 → 拒绝（home 目录访问）
- 绝对路径 → 必须在允许列表内
- 包含 `..` 段 → 拒绝

### 根路径命令验证

`_validate_local_bash_root_path_args(command_name, tokens, start_index)` 对 `awk`、`cat`、`find`、`grep`、`head`、`sed`、`tail` 等命令检查其参数中是否包含裸根路径 `/`：

```python
_LOCAL_BASH_ROOT_PATH_COMMANDS = {
    "awk", "cat", "cp", "du", "find", "grep",
    "head", "less", "ln", "ls", "more", "mv",
    "rm", "sed", "tail", "tar",
}
```

### 绝对路径正则扫描

```python
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![:\w])(?<!:/)/(?:[^\s\"'`;&|<>()]+)")
```

扫描命令中所有绝对路径，排除 URL 上下文中的路径（通过 `_non_file_url_spans` 标记 HTTP/HTTPS URL 范围）。每个绝对路径通过 `_is_allowed_local_bash_absolute_path()` 检查：

**允许的绝对路径**：
- `/mnt/user-data/...` — 用户数据路径
- `/mnt/skills/...` — 技能路径
- `/mnt/acp-workspace/...` — ACP 工作空间
- 自定义挂载路径
- MCP 文件系统服务器配置的允许路径
- 系统路径前缀：`/bin/`、`/usr/bin/`、`/usr/sbin/`、`/sbin/`、`/opt/homebrew/bin/`、`/dev/`

### 不安全路径报告

发现不安全路径时，收集所有违规路径并去重后一次性报告：

```
PermissionError: Unsafe absolute paths in command: /etc/passwd, /var/log. Use paths under /mnt/user-data
```

## 宿主 Bash 安全

`security.py` 提供 LocalSandboxProvider 的安全策略判断：

**`uses_local_sandbox_provider(config)`** — 检查当前配置是否使用 LocalSandboxProvider。通过比对配置中的 `sandbox.use` 字段与已知标记字符串判断。

**`is_host_bash_allowed(config)`** — 返回是否允许宿主 bash 执行：
- 非 LocalSandboxProvider → 始终返回 `True`（Docker 沙箱有自己的隔离）
- LocalSandboxProvider → 读取 `sandbox.allow_host_bash` 配置项（默认 `False`）

**禁止消息**：

```python
LOCAL_HOST_BASH_DISABLED_MESSAGE = (
    "Host bash execution is disabled for LocalSandboxProvider because it is not a secure "
    "sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or set "
    "sandbox.allow_host_bash: true only in a fully trusted local environment."
)
```

`bash_tool` 在 LocalSandbox 模式下首先检查 `is_host_bash_allowed()`，如果禁止则直接返回错误消息，不执行命令。

## 只读挂载强制

`LocalSandbox._is_read_only_path(resolved_path)` 在文件写入操作前检查目标路径是否属于只读挂载：

- 遍历所有 `PathMapping`，找到最长前缀匹配的映射
- 如果映射的 `read_only=True`，拒绝写入操作（抛出 `OSError(EROFS)`）

只读挂载包括：
- `/mnt/skills` — 技能目录始终只读
- `/mnt/acp-workspace` — ACP 工作空间只读
- 配置中声明 `read_only: true` 的自定义挂载

`_is_resolved_path_read_only(resolved)` 同时检查直接映射和嵌套映射的只读状态。

## 文件操作锁

`file_operation_lock.py` 提供基于 `threading.Lock` 的文件级并发控制：

```python
def get_file_operation_lock(sandbox: Sandbox, path: str) -> threading.Lock:
    lock_key = (sandbox_id, path)  # per-sandbox per-path
    # 使用 WeakValueDictionary 存储锁
    # 锁在不再被引用时自动清理
```

**锁的作用**：`write_file_tool` 和 `str_replace_tool` 在操作同一文件时获取锁，确保 read-modify-write 操作的原子性。锁的粒度是 `(sandbox_id, path)`，不同沙箱实例操作相同虚拟路径不会互相阻塞。

**内存管理**：使用 `WeakValueDictionary` 存储锁实例，防止长期运行进程中锁对象的内存泄漏。全局 `_FILE_OPERATION_LOCKS_GUARD` 锁保护锁字典本身的并发访问。

## Bash 命令安全审计中间件

`SandboxAuditMiddleware` 是 Agent 中间件链中的一环，对所有 `bash` 工具调用进行安全审计：

### 命令分类

命令经过正则 + shlex 分析后被分为三级：

| 等级 | 处理 | 示例命令 |
|------|------|----------|
| **block** | 阻止执行，返回错误 ToolMessage | `rm -rf /`、`curl url \| bash`、`dd if=`、`:(){ :\|:& };:` |
| **warn** | 执行但追加警告到输出 | `pip install`、`chmod 777`、`sudo` |
| **pass** | 正常执行 | 普通命令 |

### 高风险模式

高风险模式覆盖：
- 递归删除根目录 / home / root
- `dd` / `mkfs` 磁盘操作
- 读取 `/etc/shadow`
- 覆盖系统文件
- 管道到 `sh`/`bash`
- 命令替换中的危险命令（`curl`、`wget`、`base64`）
- base64 解码管道执行
- 覆盖 shell 启动文件
- `/proc/*/environ` 进程环境泄露
- `LD_PRELOAD` / `LD_LIBRARY_PATH` 动态链接器劫持
- `/dev/tcp/` bash 内建网络
- Fork 炸弹

### 中风险模式

- `chmod 777`
- `pip install` / `pip3 install`
- `apt install` / `apt-get install`
- `sudo` / `su`
- `PATH=` 修改

### 输入消毒

在正则分析之前，先进行输入合法性检查：
- 空命令 → 拒绝
- 长度超过 10,000 字符 → 拒绝（几乎肯定是 payload 注入）
- 包含 null 字节 → 拒绝

### 审计日志

每个 bash 调用都生成结构化 JSON 审计记录，包含时间戳、thread_id、命令（超长截断至 200 字符）和分类结果。日志通过标准 logger 输出，可在 `langgraph.log` 中查看。

### 复合命令处理

`_split_compound_command(command)` 将复合命令（`cmd1 && cmd2 ; cmd3`）按 shell 控制运算符拆分为子命令，对每个子命令独立分类。最终取最高危险等级作为整体评级。但多语句模式（如 fork 炸弹 `:(){ :|:& };:`）会在全命令级别先扫描一遍，避免拆分破坏模式上下文。
