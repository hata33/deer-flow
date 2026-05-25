# 沙箱工具

沙箱工具是 Agent 直接调用的 LangChain `@tool` 函数，定义于 `sandbox/tools.py`。每个工具封装了沙箱初始化、路径验证、路径转换、输出屏蔽等横切逻辑，向 Agent 暴露简洁的虚拟路径接口。

## 惰性沙箱初始化

所有工具通过 `ensure_sandbox_initialized(runtime)` 获取沙箱实例，该函数实现了惰性初始化模式：

1. 检查 `runtime.state["sandbox"]` 是否已有 `sandbox_id`
2. 若有，通过 `get_sandbox_provider().get(sandbox_id)` 查询
3. 若无或已释放，从 `runtime.context` 提取 `thread_id`
4. 调用 `provider.acquire(thread_id)` 获取新沙箱
5. 将 `sandbox_id` 写入 `runtime.state` 和 `runtime.context`

这保证了工具调用时沙箱一定可用，且首个工具调用触发沙箱创建。

`ensure_thread_directories_exist(runtime)` 在首次工具使用时确保线程目录（workspace/uploads/outputs）在文件系统上存在，仅对 LocalSandbox 有效。

## bash 工具

```python
@tool("bash")
def bash_tool(runtime, description, command) -> str
```

**执行流程**：

1. `ensure_sandbox_initialized(runtime)` 获取沙箱
2. LocalSandbox 模式下：
   - `is_host_bash_allowed()` 检查是否允许宿主 bash（默认禁止）
   - `validate_local_bash_command_paths(command, thread_data)` 验证命令中的路径安全性
   - `replace_virtual_paths_in_command(command, thread_data)` 将虚拟路径替换为物理路径
   - `_apply_cwd_prefix(command, thread_data)` 在命令前添加 `cd <workspace> &&`
   - 执行命令
   - `mask_local_paths_in_output()` 屏蔽输出中的物理路径
3. AioSandbox 模式下直接执行（路径已通过卷挂载映射）

**路径转换** — `replace_virtual_paths_in_command` 按顺序替换四类路径：
- `/mnt/skills/...` → `_resolve_skills_path()`
- `/mnt/acp-workspace/...` → `_resolve_acp_workspace_path()`
- `/mnt/user-data/...` → `replace_virtual_path()` 使用线程数据映射
- 自定义挂载路径由 `LocalSandbox._resolve_paths_in_command()` 处理

**CWD 前缀** — `_apply_cwd_prefix()` 在命令前添加 `cd <workspace_path> &&`，使相对路径操作锚定到线程工作空间。

**输出截断** — `_truncate_bash_output(output, max_chars)` 采用中间截断策略（50/50 头尾保留），因为 bash 输出的错误可能出现在开头或结尾。默认上限 20,000 字符。

## read_file 工具

```python
@tool("read_file")
def read_file_tool(runtime, description, path, start_line=None, end_line=None) -> str
```

**执行流程**：

1. LocalSandbox 模式：`validate_local_tool_path(path, thread_data, read_only=True)` 验证路径合法性
2. 根据路径类型选择解析策略：
   - `/mnt/skills/...` → `_resolve_skills_path()`
   - `/mnt/acp-workspace/...` → `_resolve_acp_workspace_path()`
   - 自定义挂载 → 由 `LocalSandbox._resolve_path()` 处理
   - `/mnt/user-data/...` → `_resolve_and_validate_user_data_path()`
3. `sandbox.read_file(path)` 读取内容
4. `start_line` / `end_line` 可选行范围切片（1-indexed，闭区间）

**输出截断** — `_truncate_read_file_output(output, max_chars)` 采用头部截断，保留文件开头（导入、类定义、函数签名等关键内容）。默认上限 50,000 字符。截断标记提示使用 `start_line/end_line` 参数读取指定范围。

## write_file 工具

```python
@tool("write_file")
def write_file_tool(runtime, description, path, content, append=False) -> str
```

**执行流程**：

1. LocalSandbox 模式：`validate_local_tool_path(path, thread_data)` 验证路径（不带 `read_only`，允许写入）
2. 非 `/mnt/user-data/` 的自定义挂载路径由 `LocalSandbox._resolve_path()` 处理
3. `_resolve_and_validate_user_data_path()` 解析并验证用户数据路径
4. `get_file_operation_lock(sandbox, path)` 获取文件操作锁
5. `sandbox.write_file(path, content, append)` 写入文件

**文件操作锁** — `get_file_operation_lock()` 返回一个 per-(sandbox_id, path) 的 `threading.Lock`，防止 `str_replace` 和 `write_file` 对同一文件产生竞态条件。锁存储在 `WeakValueDictionary` 中，沙箱实例释放后自动清理。

**路径验证** — 写入操作不允许访问只读路径（`/mnt/skills`、`/mnt/acp-workspace`）。`validate_local_tool_path` 在不带 `read_only=True` 时会拒绝这些路径。

## str_replace 工具

```python
@tool("str_replace")
def str_replace_tool(runtime, description, path, old_str, new_str, replace_all=False) -> str
```

**执行流程**：

1. 路径验证和解析（同 write_file）
2. `get_file_operation_lock(sandbox, path)` 获取文件操作锁
3. `sandbox.read_file(path)` 读取当前内容
4. 检查 `old_str` 是否存在于内容中
5. `replace_all=False` 时替换首次出现，`replace_all=True` 时替换全部
6. `sandbox.write_file(path, content)` 写回

锁的作用域覆盖读取和写入的完整过程，确保原子性。锁的键是 `(sandbox_id, path)` 元组，因此不同沙箱实例即使使用相同虚拟路径也不会互相阻塞。

## ls 工具

```python
@tool("ls")
def ls_tool(runtime, description, path) -> str
```

**执行流程**：

1. LocalSandbox 模式：`validate_local_tool_path(path, thread_data, read_only=True)` 验证路径
2. 根据路径类型解析（同 read_file）
3. `sandbox.list_dir(path)` 获取目录条目
4. `mask_local_paths_in_output()` 屏蔽条目中的物理路径（仅 LocalSandbox）
5. 空目录返回 `(empty)`

**输出截断** — `_truncate_ls_output(output, max_chars)` 采用头部截断，保留目录结构开头。默认上限 20,000 字符。

## glob 工具

```python
@tool("glob")
def glob_tool(runtime, description, pattern, path, include_dirs=False, max_results=200) -> str
```

**执行流程**：

1. LocalSandbox 模式：`_resolve_local_read_path(path, thread_data)` 验证并解析路径
2. `sandbox.glob(path, pattern, include_dirs, max_results)` 搜索
3. 匹配路径列表中的每条结果都经过 `mask_local_paths_in_output()` 屏蔽
4. `_format_glob_results()` 格式化输出

**max_results 控制** — 通过 `_resolve_max_results()` 取用户请求值与配置上限的最小值。默认 200，硬上限 1,000。可通过 `config.yaml` 的 tool_config 调整。

## grep 工具

```python
@tool("grep")
def grep_tool(runtime, description, pattern, path, glob=None,
              literal=False, case_sensitive=False, max_results=100) -> str
```

**执行流程**：

1. 路径验证和解析（同 glob）
2. `sandbox.grep(path, pattern, ...)` 搜索
3. 每条 `GrepMatch` 的 `path` 字段经过 `mask_local_paths_in_output()` 屏蔽
4. `_format_grep_results()` 格式化为 `{path}:{line_number}: {line}` 格式

**max_results 控制** — 默认 100，硬上限 500。`literal=True` 时对 pattern 进行 `re.escape()`。`glob` 参数可选地预过滤候选文件。

## 输出截断策略

三种工具采用不同的截断策略，匹配各自输出的特征：

| 工具 | 策略 | 原因 | 默认上限 | 截断标记提示 |
|------|------|------|----------|-------------|
| `bash` | 中间截断（50/50 头尾） | 错误可能在开头或结尾 | 20,000 字符 | `middle truncated: N chars skipped` |
| `read_file` | 头部截断 | 关键内容通常在文件开头 | 50,000 字符 | `Use start_line/end_line to read a specific range` |
| `ls` | 头部截断 | 目录结构从上到下阅读 | 20,000 字符 | `Use a more specific path to see fewer results` |

所有截断函数支持 `max_chars=0` 禁用截断。截断标记（含数字占位符）的长度在计算保留内容时已精确扣除，确保最终字符串严格不超过限制。

截断上限可通过 `config.yaml` 的 `sandbox` 配置节调整：

```yaml
sandbox:
  bash_output_max_chars: 20000
  read_file_output_max_chars: 50000
  ls_output_max_chars: 20000
```

## 路径解析辅助函数

工具层使用的几个关键路径解析函数：

**`replace_virtual_path(path, thread_data)`** — 将 `/mnt/user-data/...` 虚拟路径替换为线程实际路径。使用 `_thread_virtual_to_actual_mappings()` 构建映射表，按最长前缀优先替换。

**`_resolve_local_read_path(path, thread_data)`** — 只读工具（glob/grep）的统一路径解析入口。依次尝试 skills → acp-workspace → user-data。

**`_resolve_and_validate_user_data_path(path, thread_data)`** — 解析虚拟路径并验证解析后的路径仍在允许的根目录（workspace/uploads/outputs）内。

**`mask_local_paths_in_output(output, thread_data)`** — 输出屏蔽的完整实现，处理四类路径：
1. Skills 宿主路径 → `/mnt/skills/...`
2. ACP workspace 宿主路径 → `/mnt/acp-workspace/...`
3. 用户数据宿主路径 → `/mnt/user-data/...`
4. 自定义挂载路径 → 由 `LocalSandbox._reverse_resolve_paths_in_output()` 处理

对每类路径，同时处理原始路径和 `Path.resolve()` 后的路径，并兼容正斜杠和反斜杠两种分隔符。
