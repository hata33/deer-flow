# 完整生命周期

沙箱的生命周期贯穿 Agent 会话的始终——从首次工具调用时创建，跨多个对话轮次复用，直到应用关闭或配置变更时清理。本章详细描述这一过程的每个阶段。

## 阶段总览

```
┌────────────────────────────────────────────────────────────────────┐
│                        应用启动                                     │
│  config.yaml → get_sandbox_provider() → 单例 SandboxProvider      │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                   会话开始（新线程）                                  │
│  SandboxMiddleware.before_agent() → provider.acquire(thread_id)   │
│  或惰性初始化：ensure_sandbox_initialized() → provider.acquire()   │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                     工具执行（每轮对话多次）                           │
│  ensure_sandbox_initialized() → 查询已有沙箱                       │
│  ensure_thread_directories_exist() → 创建线程目录                   │
│  sandbox.method() → 执行操作                                       │
│  mask_local_paths_in_output() → 输出屏蔽                           │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                      轮次结束                                       │
│  SandboxMiddleware.after_agent() → provider.release() (本地模式无操作)│
│  沙箱保留在缓存中，下一轮直接复用                                     │
└───────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                     会话结束 / 应用关闭                               │
│  shutdown_sandbox_provider() → provider.shutdown() → 清理缓存      │
└────────────────────────────────────────────────────────────────────┘
```

## 沙箱获取

### SandboxMiddleware 触发

`SandboxMiddleware` 是 Agent 中间件链的第三个组件，负责沙箱的获取和释放。

**惰性初始化模式**（`lazy_init=True`，默认）：
- `before_agent()` 不主动获取沙箱
- 沙箱在首个工具调用时由 `ensure_sandbox_initialized()` 惰性创建
- 减少无工具调用轮次的开销

**急切初始化模式**（`lazy_init=False`）：
- `before_agent()` 在 Agent 调用前立即获取沙箱
- 从 `runtime.context["thread_id"]` 提取线程 ID
- 调用 `provider.acquire(thread_id)` 获取沙箱
- 将 `sandbox_id` 写入 `state["sandbox"]`

### LocalSandboxProvider.acquire

`acquire(thread_id)` 的执行路径：

**`thread_id=None`（无线程上下文）**：
- 返回单例沙箱，id 为 `"local"`
- 用于旧版测试和脚本

**`thread_id="abc"`（有线程上下文）**：
1. 检查 `_thread_sandboxes` LRU 缓存是否已有该 thread_id 的沙箱
2. 如果有，提升到最近使用位置并返回
3. 如果没有，在锁外调用 `_build_thread_path_mappings(thread_id)` 构建路径映射（涉及文件系统 I/O）
4. 获取锁后再次检查缓存（防并发重复创建）
5. 创建新 `LocalSandbox(f"local:{thread_id}", path_mappings)`
6. 插入 LRU 缓存，必要时淘汰最久未使用的条目

### 线程路径映射构建

`_build_thread_path_mappings(thread_id)` 为每个线程构建五条路径映射：

| 容器路径 | 本地路径 | 只读 |
|----------|----------|------|
| `/mnt/user-data` | `{sandbox_user_data_dir}` | 否 |
| `/mnt/user-data/workspace` | `{sandbox_work_dir}` | 否 |
| `/mnt/user-data/uploads` | `{sandbox_uploads_dir}` | 否 |
| `/mnt/user-data/outputs` | `{sandbox_outputs_dir}` | 否 |
| `/mnt/acp-workspace` | `{acp_workspace_dir}` | 否 |

父级 `/mnt/user-data` 映射使 `ls /mnt/user-data` 能显示三个子目录，与 AIO 容器行为一致。子路径映射更长，在 `_find_path_mapping` 的降序排序中优先匹配。

静态映射（skills 目录、自定义挂载）在 Provider 构造时一次性构建，每个线程沙箱继承这些静态映射并追加线程专属映射。

## LRU 缓存管理

`LocalSandboxProvider` 使用 `OrderedDict` 实现 LRU 缓存：

- **容量**：默认 256 个线程沙箱（`DEFAULT_MAX_CACHED_THREAD_SANDBOXES`）
- **淘汰策略**：当缓存超过容量时，从头部弹出最久未使用的条目
- **提升操作**：每次 `acquire` 和 `get` 都将访问的条目移到尾部
- **淘汰代价**：被淘汰线程下次 `acquire` 时需重建沙箱，丢失 `_agent_written_paths` 集合（read_file 退化到不做反向解析）

线程安全通过 `threading.Lock` 保证，所有缓存读写操作都在锁保护下执行。路径映射构建（涉及文件系统 I/O）在锁外完成，避免长时间持锁。

## 工具执行流程

每个沙箱工具的执行遵循统一模式：

```
ensure_sandbox_initialized(runtime)
  │
  ├── state["sandbox"] 已有 sandbox_id → provider.get() 查询
  │     └── 有 → 直接返回沙箱实例
  └── 无或已释放 → 从 context/config 提取 thread_id
        └── provider.acquire(thread_id) → 写入 state["sandbox"]

ensure_thread_directories_exist(runtime)
  │
  ├── 非 LocalSandbox → 跳过
  └── LocalSandbox → 创建 workspace/uploads/outputs 目录（如果不存在）

[路径验证] → [路径解析] → [沙箱操作] → [输出屏蔽]
```

**沙箱复用**：同一轮次内的多个工具调用共享同一个沙箱实例。`ensure_sandbox_initialized` 在后续调用中直接返回已有实例。

## 沙箱复用与释放

### 跨轮次复用

`SandboxMiddleware.after_agent()` 在 Agent 调用结束后尝试释放沙箱，但 `LocalSandboxProvider.release()` 是一个空操作——本地沙箱没有需要清理的资源，缓存实例被保留以支持跨轮次复用。

保留缓存的核心原因是 `_agent_written_paths` 集合。这个集合跟踪了 Agent 通过 `write_file` 写入的文件路径，使得后续 `read_file` 能对这些文件进行反向路径解析。如果每轮释放沙箱，这个集合就会丢失。

### AioSandboxProvider 的差异

Docker 沙箱（AioSandboxProvider）有不同的生命周期策略：
- 每个线程分配独立容器
- 有空闲超时机制（默认 10 分钟）
- 有最大副本数限制（默认 3，超出时 LRU 淘汰）
- 容器有真实资源开销，需要及时释放

## 沙箱重置与关闭

### 配置变更重置

`reset_sandbox_provider()` 清除全局单例并调用 `provider.reset()`：

- `LocalSandboxProvider.reset()` 清空所有缓存（generic + thread sandboxes），重置模块级 `_singleton`
- 下次 `get_sandbox_provider()` 创建新实例时会重新加载配置
- 重建的沙箱获得新的 `_setup_path_mappings()`，反映最新的 mounts 配置

**注意**：`reset()` 会导致所有缓存的 `_agent_written_paths` 丢失。活跃线程的下一次 read_file 不会对文件内容做反向路径解析。

### 应用关闭

`shutdown_sandbox_provider()` 在重置前额外调用 `provider.shutdown()`：

- `LocalSandboxProvider.shutdown()` 委托给 `reset()`
- AioSandboxProvider 额外停止 Docker 容器

### 单例管理

```python
_default_sandbox_provider: SandboxProvider | None = None

def get_sandbox_provider(**kwargs) -> SandboxProvider:
    # 首次调用时创建，后续调用返回缓存实例

def reset_sandbox_provider() -> None:
    # 清空缓存但不关闭资源

def shutdown_sandbox_provider() -> None:
    # 关闭资源后清空缓存

def set_sandbox_provider(provider: SandboxProvider) -> None:
    # 注入自定义 Provider（测试用）
```

## 错误处理链

沙箱系统的异常层次结构定义于 `exceptions.py`：

```
SandboxError                      ← 基类，包含 message + details 字典
├── SandboxNotFoundError          ← 沙箱未找到（包含 sandbox_id）
├── SandboxRuntimeError           ← 运行时配置错误
├── SandboxCommandError           ← 命令执行失败（包含 command + exit_code）
└── SandboxFileError              ← 文件操作失败（包含 path + operation）
    ├── SandboxPermissionError    ← 权限错误
    └── SandboxFileNotFoundError  ← 文件未找到
```

工具函数的异常处理策略：

1. **`SandboxError` 子类** → 返回 `Error: {e}`
2. **`PermissionError`** → 返回 `Error: Permission denied: {path}`
3. **`FileNotFoundError`** → 返回 `Error: File not found: {path}`
4. **其他异常** → 返回 `Error: Unexpected error ...: {_sanitize_error(e, runtime)}`

`_sanitize_error()` 在 LocalSandbox 模式下通过 `mask_local_paths_in_output()` 清理错误消息中的物理路径，防止宿主机路径泄露。

所有工具的异常都不会中断 Agent 对话流——错误被转换为文本消息返回给 LLM，LLM 可以根据错误信息调整策略重试。

## 配置结构

`SandboxConfig`（`config/sandbox_config.py`）定义沙箱配置模型：

| 字段 | 默认值 | 用途 |
|------|--------|------|
| `use` | （必需） | Provider 类路径 |
| `allow_host_bash` | `false` | 允许本地沙箱执行宿主 bash |
| `image` | `null` | Docker 镜像（AioSandbox 专用） |
| `replicas` | `null` | 最大并发容器数（默认 3） |
| `idle_timeout` | `null` | 空闲超时秒数（默认 600） |
| `mounts` | `[]` | 自定义卷挂载列表 |
| `environment` | `{}` | 容器环境变量 |
| `bash_output_max_chars` | `20000` | bash 输出截断上限 |
| `read_file_output_max_chars` | `50000` | read_file 输出截断上限 |
| `ls_output_max_chars` | `20000` | ls 输出截断上限 |

`VolumeMountConfig` 定义单条挂载规则：`host_path` + `container_path` + `read_only`。

`SandboxConfig` 使用 `extra="allow"` 允许 Provider 特定字段透传，确保不同实现可以扩展自己的配置项。
