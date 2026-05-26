# 05 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **虚拟路径 /mnt/ 抽象层** | 环境无关性：Agent 代码不依赖宿主机路径 |
| 2 | **SandboxProvider + Sandbox 两层接口** | 生命周期（创建/释放）与操作（读写/执行）分离 |
| 3 | **Local 使用 PathMapping + LRU 缓存** | 零拷贝路径翻译，O(1) 缓存查找 |
| 4 | **str_replace 锁粒度为 (sandbox.id, path)** | 避免跨沙箱锁竞争，同文件操作串行化 |
| 5 | **read_file 默认不做行号**，由工具层截断 | 减少存储开销，按需通过 start_line/end_line 读取范围 |
| 6 | **路径安全纵深防御** | 单点绕过不导致整体沦陷 |

---

## 二、逐决策分析

### 决策 1：虚拟路径 /mnt/ 抽象层

**问题**：Agent 在不同部署环境（本地开发、Docker 容器、远程集群）中操作文件，路径完全不同。直接使用宿主机路径会导致 Agent 输出不可移植，且暴露宿主机目录结构。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 虚拟路径 /mnt/ 抽象（当前） | Agent 代码零修改跨环境；隐藏宿主机结构 | 需双向路径翻译层 |
| 直接暴露宿主机路径 | 无翻译开销 | Agent 输出不可移植；安全风险 |
| 环境变量注入路径 | 灵活 | Agent prompt 需感知环境变量 |

**选择虚拟路径**：所有 Sandbox 方法的 path 参数使用统一的虚拟路径前缀（`/mnt/user-data/`、`/mnt/skills/`、`/mnt/acp-workspace/`）。Agent 和 prompt 只看到这些路径，由 PathMapping 在执行时翻译为宿主机真实路径。输出中也自动反向替换（mask_local_paths_in_output），确保宿主机路径不会泄露。

核心映射规则：

| 虚拟路径 | 宿主机路径 | 权限 |
|---------|-----------|------|
| `/mnt/user-data/workspace/` | `{base}/users/{uid}/threads/{tid}/user-data/workspace/` | 读写 |
| `/mnt/user-data/uploads/` | `{base}/users/{uid}/threads/{tid}/user-data/uploads/` | 读写 |
| `/mnt/user-data/outputs/` | `{base}/users/{uid}/threads/{tid}/user-data/outputs/` | 读写 |
| `/mnt/skills/` | `{project}/skills/` | 只读 |
| `/mnt/acp-workspace` | `{base}/users/{uid}/threads/{tid}/acp-workspace/` | 读写 |

---

### 决策 2：SandboxProvider + Sandbox 两层接口

**问题**：沙箱的生命周期管理（创建、缓存、释放）与文件/命令操作（读写、搜索）的复杂度完全不同，混在一起会导致职责不清。

| 方案 | 优势 | 劣势 |
|------|------|------|
| Provider + Sandbox 两层（当前） | 单一职责；Provider 可切换实现；Sandbox 可独立测试 | 接口数量多 |
| 单一大接口 | 简单 | 生命周期与操作耦合 |

**选择两层分离**：

- `SandboxProvider`（抽象基类）：负责 `acquire(thread_id)` 创建沙箱、`get(sandbox_id)` 查找、`release()` 释放。全局单例模式（`get_sandbox_provider()`），通过 `config.yaml` 的 `sandbox.use` 动态加载实现类。
- `Sandbox`（抽象基类）：定义 `execute_command`、`read_file`、`write_file`、`list_dir`、`glob`、`grep` 等纯操作接口。每个实现（Local、AIO）独立处理路径映射。

Provider 的 `uses_thread_data_mounts` 属性标识是否使用每线程数据挂载，影响 ThreadDataMiddleware 的行为。

---

### 决策 3：Local 使用 PathMapping + LRU 缓存

**问题**：每个对话线程有独立的宿主机目录（`/users/{uid}/threads/{tid}/`），LocalSandbox 需要为每个线程维护路径映射。长期运行进程中线程数无上限增长。

| 方案 | 优势 | 劣势 |
|------|------|------|
| PathMapping + LRU 缓存（当前） | 自动淘汰冷门线程；内存可控 | LRU 淘汰丢失 `_agent_written_paths` |
| 每次 acquire 重建 PathMapping | 无缓存一致性问题 | 每次工具调用都做文件系统 I/O |
| 无上限字典 | 无淘汰开销 | 内存泄漏风险 |

**选择 PathMapping + LRU**：`LocalSandboxProvider` 使用 `OrderedDict` 实现 LRU 缓存（默认 256 条）。`acquire()` 查缓存 → 命中则 `move_to_end()`；未命中则构建 PathMapping 并插入。`_build_thread_path_mappings()` 涉及 `ensure_thread_dirs()`（文件系统 I/O），在锁外执行避免阻塞其他线程，完成后再加锁检查 double-insert。

LRU 淘汰的影响有限：仅丢失 `_agent_written_paths`（read_file 不再做反向路径解析），与全新运行时行为一致。下次 acquire 自动重建。

---

### 决策 4：str_replace 锁粒度为 (sandbox.id, path)

**问题**：多线程环境下，多个工具调用可能同时 `str_replace` 同一文件，导致内容交错损坏。

| 方案 | 优势 | 劣势 |
|------|------|------|
| (sandbox.id, path) 粒度锁（当前） | 精确串行化；不同文件/沙箱不互斥 | 全局锁映射表内存开销 |
| 全局单锁 | 最简单 | 所有文件操作串行化 |
| per-sandbox 锁 | 减少映射表大小 | 同沙箱不同文件互斥 |

**选择 (sandbox.id, path)**：`file_operation_lock.py` 使用 `WeakValueDictionary` 存储 `(sandbox_id, path) → Lock` 映射。弱引用确保无引用的锁被 GC 回收，避免内存泄漏。全局 `_FILE_OPERATION_LOCKS_GUARD` 互斥锁保护锁的 get-or-create 操作。

不同沙箱（不同 `thread_id`）操作同一路径互不影响；同沙箱内同文件操作被串行化。`str_replace` 在 `get_file_operation_lock(sandbox, path)` 获取的锁内执行 read + replace + write 原子操作。

---

### 决策 5：read_file 默认不做行号

**问题**：LLM 在查看代码时需要行号来定位修改位置，但 read_file 的 Sandbox 接口返回原始文本。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 工具层截断 + start_line/end_line（当前） | 节省 token；按需读取 | LLM 需主动请求行范围 |
| 默认 sed 风格行号 | LLM 直观定位 | 每行增加 5-7 字符开销 |

**当前选择**：`read_file_tool` 通过 `start_line`/`end_line` 参数支持范围读取，`_truncate_read_file_output` 做 head-truncation 并提示使用范围参数。Sandbox 层返回纯净文本，行号格式化留给上层。

`_truncate_read_file_output` 使用 head-truncation（保留文件头部），因为源码文件的头部包含 imports、类定义和函数签名，信息密度最高。`_truncate_bash_output` 使用 middle-truncation（50/50 分割），因为 bash 输出的错误可能在 stderr（头部）或末尾。

---

### 决策 6：路径安全纵深防御

**问题**：本地沙箱不是真正的隔离边界（命令直接在宿主机执行），任何单点安全检查被绕过都可能导致宿主机文件泄露。

**选择纵深防御**：五层安全检查，任何一层被绕过不影响其他层：

| 层级 | 检查点 | 位置 |
|------|--------|------|
| 1 | `validate_local_tool_path()` — 虚拟路径前缀白名单 | tools.py |
| 2 | `_reject_path_traversal()` — 拒绝 `..` 段 | tools.py |
| 3 | `_validate_resolved_user_data_path()` — 解析后路径仍在允许根目录内 | tools.py |
| 4 | `LocalSandbox._resolve_path_with_mapping()` — PathMapping 边界检查 | local_sandbox.py |
| 5 | `validate_local_bash_command_paths()` — bash 命令中绝对路径白名单 | tools.py |

bash 命令额外安全措施：阻止 `file://` URL、`cd`/`pushd` 只允许虚拟路径目标、shell token 级别的 `..` 检测、URL span 排除（不把 `http://` 中的 `/` 当路径）。

本地沙箱默认禁止 bash 执行（`sandbox.allow_host_bash: false`），需显式启用。AIO 容器沙箱无此限制。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **环境无关** | Agent 只看到 `/mnt/` 虚拟路径，跨本地/Docker 部署无修改 |
| **线程隔离** | 每个 thread_id 有独立 LocalSandbox 和 PathMapping |
| **零拷贝性能** | LRU 缓存 + 最长前缀匹配，路径翻译无文件系统 I/O |
| **文件安全** | 五层纵深防御 + WeakValueDictionary 锁映射 |
| **输出安全** | mask_local_paths_in_output 自动替换宿主机路径为虚拟路径 |
| **可扩展** | SandboxProvider 通过 config.yaml 动态加载实现类 |
