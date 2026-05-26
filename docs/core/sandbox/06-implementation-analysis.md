# 06 - 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/sandbox/` 目录下的源码，逐层拆解沙箱系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方（Agent 工具层）                       │
│                                                                  │
│  sandbox/tools.py                                                │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ bash_tool  ls_tool  read_file_tool  write_file_tool      │   │
│  │ glob_tool  grep_tool  str_replace_tool                   │   │
│  │                                                           │   │
│  │  ensure_sandbox_initialized() → get_sandbox_provider()   │   │
│  │  replace_virtual_path() / replace_virtual_paths_in_command()│  │
│  │  validate_local_tool_path() / validate_local_bash_command_paths()│
│  │  mask_local_paths_in_output()                             │   │
│  └──────────┬──────────────────────────┬─────────────────────┘   │
│             │                          │                         │
│             │ ①获取沙箱实例             │ ②路径翻译/安全校验      │
└─────────────┼──────────────────────────┼─────────────────────────┘
              │                          │
┌─────────────▼──────────────────────────▼─────────────────────────┐
│                      sandbox 包（核心层）                         │
│                                                                   │
│  __init__.py ─── 统一导出 Sandbox, SandboxProvider               │
│                                                                   │
│  ┌──────────────────┐   ┌───────────────────────────┐           │
│  │ sandbox_provider  │   │ sandbox.py (抽象基类)      │           │
│  │                  │   │                           │           │
│  │ acquire/get/     │   │ execute_command           │           │
│  │ release/reset    │   │ read_file/write_file      │           │
│  │                  │   │ list_dir/glob/grep        │           │
│  │ 全局单例管理      │   └─────────┬─────────────────┘           │
│  └────────┬─────────┘             │                             │
│           │                       │ 子类实现                     │
│  ┌────────▼───────────────────────▼──────────────────┐         │
│  │ local/ 子包                                        │         │
│  │                                                    │         │
│  │ local_sandbox_provider.py  ─ LRU 缓存 + 线程锁    │         │
│  │   └─ _thread_sandboxes: OrderedDict                │         │
│  │   └─ _build_thread_path_mappings()                 │         │
│  │                                                    │         │
│  │ local_sandbox.py  ── PathMapping 双向解析           │         │
│  │   └─ _resolve_path() / _reverse_resolve_path()    │         │
│  │   └─ _resolve_paths_in_command()                   │         │
│  │   └─ _reverse_resolve_paths_in_output()           │         │
│  │                                                    │         │
│  │ list_dir.py  ── 递归目录遍历（深度控制）             │         │
│  └────────────────────────────────────────────────────┘         │
│                                                                   │
│  ┌──────────────────┐   ┌───────────────────────┐               │
│  │ security.py      │   │ file_operation_lock.py│               │
│  │ bash 执行限制     │   │ (sandbox_id, path) 锁 │               │
│  │ 沙箱类型检测      │   │ WeakValueDictionary   │               │
│  └──────────────────┘   └───────────────────────┘               │
│                                                                   │
│  ┌──────────────────┐   ┌───────────────────────┐               │
│  │ exceptions.py    │   │ search.py             │               │
│  │ 分层异常结构      │   │ glob/grep 匹配        │               │
│  └──────────────────┘   └───────────────────────┘               │
│                                                                   │
│  middleware.py ─── SandboxMiddleware（生命周期管理）               │
└───────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：虚拟路径翻译

### 2.1 单路径翻译：`replace_virtual_path()`

**文件**：`sandbox/tools.py`

```python
def replace_virtual_path(path: str, thread_data: ThreadDataState | None) -> str:
    # ① 从 thread_data 构建映射表
    mappings = _thread_virtual_to_actual_mappings(thread_data)
    #   映射：/mnt/user-data/workspace → {宿主workspace路径}
    #         /mnt/user-data/uploads  → {宿主uploads路径}
    #         /mnt/user-data/outputs  → {宿主outputs路径}
    #         /mnt/user-data          → {宿主user-data父目录}

    # ② 最长前缀优先替换
    for virtual_base, actual_base in sorted(mappings, key=len, reverse=True):
        if path == virtual_base:          return actual_base
        if path.startswith(virtual_base + "/"):
            return actual_base + rest     # 拼接相对路径

    return path  # 无匹配，原样返回
```

**为什么 tools.py 和 LocalSandbox 都有路径翻译**：tools.py 的 `replace_virtual_path()` 用于路径校验和 bash 命令翻译，是防线之一。LocalSandbox 的 `_resolve_path()` 是 PathMapping 层的翻译，处理所有自定义挂载。两层独立工作，形成纵深防御。

### 2.2 命令内批量翻译：`replace_virtual_paths_in_command()`

```python
def replace_virtual_paths_in_command(command: str, thread_data) -> str:
    result = command
    # ① 替换 /mnt/skills 路径
    result = skills_pattern.sub(replace_skills_match, result)
    # ② 替换 /mnt/acp-workspace 路径
    result = acp_pattern.sub(replace_acp_match, result)
    # ③ 替换 /mnt/user-data 路径
    result = user_data_pattern.sub(replace_user_data_match, result)
    return result
```

每个替换使用正则匹配路径后跟可选子路径（`(/[^\s\"';&|<>()]*)?`），避免匹配路径中间的子串。例如 `/mnt/user-data/workspace` 不会错误匹配 `/mnt/user-data-workspace`。

### 2.3 输出屏蔽：`mask_local_paths_in_output()`

```
命令执行返回："Error: /home/user/.deer-flow/users/alice/threads/t1/user-data/workspace/test.py not found"
                  ↓ mask_local_paths_in_output()
输出给 Agent："Error: /mnt/user-data/workspace/test.py not found"
```

屏蔽三类路径：skills 路径、ACP workspace 路径、user-data 路径。每种路径处理 raw 和 resolved 两种变体（Windows 上 `C:\Users\...` vs 解析后的路径），通过 `_path_variants()` 生成所有可能的写法。

---

## 三、第 2 层：LocalSandboxProvider — 生命周期管理

### 3.1 `acquire()` 流程

```
acquire(thread_id="abc123")
  │
  ├─ thread_id is None?
  │   YES → 返回通用沙箱单例 (id="local")
  │
  ├─ 快速路径：_thread_sandboxes.get("abc123")
  │   命中 → move_to_end("abc123") → 返回 cached.id
  │
  ├─ 锁外：_build_thread_path_mappings("abc123")
  │   ├─ paths.ensure_thread_dirs()        ← 文件系统 I/O
  │   └─ 返回 /mnt/user-data/ 和 /mnt/acp-workspace 映射
  │
  └─ 加锁：
      ├─ double-check：另一个线程可能已插入
      ├─ LocalSandbox("local:abc123", path_mappings)
      ├─ _evict_until_within_cap_locked()   ← 超上限时淘汰
      └─ 返回 sandbox.id
```

**为什么锁外做文件系统 I/O**：`ensure_thread_dirs()` 创建目录，可能耗时数十毫秒。在锁内执行会阻塞所有线程的 acquire 调用。锁外完成后 double-check 确保一致性。

### 3.2 LRU 淘汰机制

```python
def _evict_until_within_cap_locked(self):
    while len(self._thread_sandboxes) > self._max_cached_threads:
        evicted_thread_id, _ = self._thread_sandboxes.popitem(last=False)
        # popitem(last=False) 移除最早插入的条目
        # 配合 move_to_end() 实现经典 LRU
```

被淘汰的沙箱仅丢失 `_agent_written_paths` 集合（read_file 不再做反向路径解析）。下次 acquire 时自动重建完整沙箱。

### 3.3 PathMapping 静态 + 动态分层

```
静态映射（所有沙箱共享）：
  /mnt/skills → {project}/skills/          (只读)
  /mnt/custom → {host_path}                (来自 config.yaml)

动态映射（每线程独立）：
  /mnt/user-data → {base}/users/{uid}/threads/{tid}/user-data/
  /mnt/user-data/workspace → .../workspace/
  /mnt/user-data/uploads   → .../uploads/
  /mnt/user-data/outputs   → .../outputs/
  /mnt/acp-workspace       → .../acp-workspace/

acquire 时合并：static_mappings + thread_mappings → LocalSandbox.path_mappings
```

---

## 四、第 3 层：LocalSandbox — 文件操作实现

### 4.1 路径解析核心：`_resolve_path_with_mapping()`

```python
def _resolve_path_with_mapping(self, path):
    mapping_match = self._find_path_mapping(path)  # 最长前缀优先
    if not mapping_match:
        return ResolvedPath(path, None)             # 无映射，原样返回

    mapping, relative = mapping_match
    local_root = Path(mapping.local_path).resolve()
    resolved = (local_root / relative).resolve()

    # 安全检查：解析后路径必须在映射根目录内
    resolved.relative_to(local_root)  # ValueError → PermissionError
    return ResolvedPath(str(resolved), mapping)
```

`_find_path_mapping()` 按 `container_path` 长度降序排列，找到第一个匹配的映射。这确保 `/mnt/user-data/workspace` 匹配 workspace 映射而非 `/mnt/user-data` 父映射。

### 4.2 命令执行：`execute_command()`

```
输入："cat /mnt/user-data/workspace/readme.md"
  │
  ├─ _resolve_paths_in_command()
  │   正则匹配容器路径 → _resolve_path() → 宿主机路径
  │   "cat /home/user/.deer-flow/.../workspace/readme.md"
  │
  ├─ _get_shell()  → 检测 zsh/bash/sh/PowerShell/cmd
  │
  ├─ subprocess.run(args, timeout=600, capture_output=True)
  │
  └─ _reverse_resolve_paths_in_output()
      正则匹配宿主机路径 → _reverse_resolve_path() → 虚拟路径
      输出中宿主机路径全部被替换
```

### 4.3 bash 工具的安全管线

```python
def bash_tool(runtime, description, command):
    sandbox = ensure_sandbox_initialized(runtime)
    if is_local_sandbox(runtime):
        if not is_host_bash_allowed():    # 安全门控
            return error_message
        ensure_thread_directories_exist(runtime)
        validate_local_bash_command_paths(command, thread_data)  # 路径白名单
        command = replace_virtual_paths_in_command(command, thread_data)  # 路径翻译
        command = _apply_cwd_prefix(command, thread_data)  # cd workspace &&
        output = sandbox.execute_command(command)
        return _truncate_bash_output(
            mask_local_paths_in_output(output, thread_data),  # 输出屏蔽
            max_chars
        )
```

### 4.4 str_replace 的原子操作

```python
def str_replace_tool(runtime, description, path, old_str, new_str, replace_all):
    sandbox = ensure_sandbox_initialized(runtime)
    # 路径解析和校验 ...
    with get_file_operation_lock(sandbox, path):  # (sandbox_id, path) 粒度锁
        content = sandbox.read_file(path)
        if old_str not in content:
            return "Error: String to replace not found"
        if replace_all:
            content = content.replace(old_str, new_str)
        else:
            content = content.replace(old_str, new_str, 1)  # 仅替换第一次出现
        sandbox.write_file(path, content)
    return "OK"
```

锁粒度为 `(sandbox.id, path)`，不同沙箱的同名虚拟路径互不影响。`WeakValueDictionary` 存储锁对象，无引用时自动 GC。

---

## 五、第 4 层：Per-Thread 隔离

### 5.1 thread_id → sandbox_id 映射

```
thread_id="abc123"  →  acquire("abc123")  →  sandbox_id="local:abc123"
                                               ↓
                                          LocalSandbox.path_mappings = [
                                            /mnt/user-data → .../threads/abc123/user-data/
                                            /mnt/user-data/workspace → .../workspace/
                                            ...
                                          ]
```

`is_local_sandbox()` 识别两种 ID 格式：`"local"`（旧版通用沙箱）和 `"local:{thread_id}"`（每线程沙箱）。

### 5.2 中间件自动注入

```
Agent 启动 → SandboxMiddleware.before_agent()
  → runtime.context["thread_id"] → provider.acquire(thread_id)
  → state["sandbox"] = {"sandbox_id": "local:abc123"}

工具调用 → ensure_sandbox_initialized(runtime)
  → runtime.state["sandbox"]["sandbox_id"]
  → provider.get("local:abc123") → LocalSandbox 实例
```

懒加载模式（默认）：SandboxMiddleware 不在 `before_agent` 中创建沙箱，而是由 `ensure_sandbox_initialized()` 在首次工具调用时触发。Agent 如果不需要文件/命令操作，则不浪费资源。

---

## 六、文件职责速查表

| 文件 | 核心职责 | 关键类/函数 |
|------|----------|------------|
| `sandbox.py` | Sandbox 抽象基类 | `Sandbox`（ABC） |
| `sandbox_provider.py` | Provider 抽象基类 + 全局单例 | `SandboxProvider`、`get_sandbox_provider()` |
| `tools.py` | Agent 工具实现 + 路径翻译 + 安全校验 | `bash_tool`、`replace_virtual_path()`、`mask_local_paths_in_output()` |
| `security.py` | 本地沙箱安全门控 | `is_host_bash_allowed()`、`uses_local_sandbox_provider()` |
| `file_operation_lock.py` | 文件级并发锁 | `get_file_operation_lock()`、`WeakValueDictionary` |
| `local/local_sandbox.py` | 本地沙箱实现 | `LocalSandbox`、`PathMapping`、`_resolve_path_with_mapping()` |
| `local/local_sandbox_provider.py` | 本地 Provider 实现 | `LocalSandboxProvider`、LRU 缓存、`_build_thread_path_mappings()` |
| `local/list_dir.py` | 递归目录遍历 | `list_dir()`（max_depth、符号链接安全） |
| `exceptions.py` | 分层异常结构 | `SandboxError` → `SandboxCommandError` / `SandboxFileError` |
| `middleware.py` | Agent 中间件 | `SandboxMiddleware`（懒加载/急切模式） |
