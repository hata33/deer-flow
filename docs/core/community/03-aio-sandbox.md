# AIO Docker 沙箱详解

> 源码路径：`backend/packages/harness/deerflow/community/aio_sandbox/`

## 概述

AIO（All-In-One）沙箱是 DeerFlow 的 Docker 容器隔离执行环境，基于 `agent-infra/sandbox` 镜像，通过 HTTP API 提供完整的文件系统操作和命令执行能力。

与核心模块的 `LocalSandbox`（本地文件系统直连）不同，AioSandbox 将所有操作隔离在 Docker 容器中，提供更强的安全边界。

## 模块结构

```
aio_sandbox/
├── __init__.py                  # 导出所有公开类
├── aio_sandbox.py               # AioSandbox 沙箱实现（Sandbox 子类）
├── aio_sandbox_provider.py      # AioSandboxProvider 生命周期管理
├── backend.py                   # SandboxBackend 抽象基类
├── local_backend.py             # 本地 Docker/Apple Container 后端
├── remote_backend.py            # 远程 K8s Provisioner 后端
└── sandbox_info.py              # SandboxInfo 元数据
```

## 类关系图

```
Sandbox (抽象基类, deerflow.sandbox.sandbox)
    │
    ├── LocalSandbox (deerflow.sandbox.local)
    │       └── 直接操作宿主机文件系统
    │
    └── AioSandbox (community.aio_sandbox.aio_sandbox)
            └── 通过 HTTP API 操作容器内文件系统

SandboxProvider (抽象基类, deerflow.sandbox.sandbox_provider)
    │
    ├── LocalSandboxProvider
    │       └── 本地文件系统沙箱
    │
    └── AioSandboxProvider (community.aio_sandbox.aio_sandbox_provider)
            │
            └── SandboxBackend (抽象)
                    │
                    ├── LocalContainerBackend
                    │       └── 本地 Docker / Apple Container
                    │
                    └── RemoteSandboxBackend
                            └── K8s Provisioner 服务
```

## SandboxInfo — 沙箱元数据

> 文件：`aio_sandbox/sandbox_info.py`

```python
@dataclass
class SandboxInfo:
    sandbox_id: str                      # 沙箱唯一标识
    sandbox_url: str                     # HTTP API 地址（如 http://localhost:8080）
    container_name: str | None = None    # 容器名（仅本地后端）
    container_id: str | None = None      # 容器 ID（仅本地后端）
    created_at: float = field(...)       # 创建时间戳
```

**序列化**：支持 `to_dict()` / `from_dict()` 方法，用于跨进程共享沙箱信息。`from_dict()` 同时兼容 `sandbox_url` 和 `base_url` 字段名。

## AioSandbox — 沙箱实例

> 文件：`aio_sandbox/aio_sandbox.py`

`AioSandbox` 继承自 `Sandbox` 抽象基类，通过 `agent_sandbox.Sandbox` 客户端与容器内的 HTTP API 通信。

### 初始化

```python
class AioSandbox(Sandbox):
    def __init__(self, id: str, base_url: str, home_dir: str | None = None)
```

| 参数 | 类型 | 说明 |
|:-----|:-----|:-----|
| `id` | `str` | 沙箱唯一标识 |
| `base_url` | `str` | 容器 API 地址（如 `http://localhost:8080`） |
| `home_dir` | `str \| None` | 容器内家目录（None 时自动获取） |

### 线程安全

`AioSandbox` 使用 `threading.Lock` 序列化所有命令执行。这是因为 AIO 沙箱容器维护**单一持久 shell 会话**，并发 `exec_command` 调用会导致会话损坏（返回 `ErrorObservation`）。

```python
def execute_command(self, command: str) -> str:
    with self._lock:
        # 执行命令...
        # 如果检测到 ErrorObservation，自动重试
```

### 超时配置

`_DEFAULT_NO_CHANGE_TIMEOUT = 600`（秒），与客户端级别超时一致，防止长时间无输出的命令被沙箱内置的 120 秒默认值过早终止。

### 核心方法

#### execute_command

```python
def execute_command(self, command: str) -> str
```

在容器内执行 shell 命令。包含 **ErrorObservation 自动重试**机制：

1. 在线程锁内执行命令
2. 检查输出是否包含 `'ErrorObservation' object has no attribute 'exit_code'` 签名
3. 如果检测到，使用新的 session ID 重试
4. 异常时返回 `"Error: {exception}"` 字符串

#### read_file / write_file

```python
def read_file(self, path: str) -> str
def write_file(self, path: str, content: str, append: bool = False) -> None
```

通过容器 API 读写文件。`write_file` 的 `append` 模式先读取现有内容再追加。

#### download_file

```python
def download_file(self, path: str) -> bytes
```

下载容器内文件的二进制内容。包含多层安全检查：

1. **路径遍历防护**：拒绝包含 `..` 段的路径
2. **路径前缀限制**：只允许访问 `VIRTUAL_PATH_PREFIX`（`/mnt/user-data`）下的文件
3. **大小限制**：文件超过 **100 MB**（`_MAX_DOWNLOAD_SIZE`）时抛出 `OSError`
4. **流式下载**：使用 chunked 传输，逐块检查大小

#### list_dir

```python
def list_dir(self, path: str, max_depth: int = 2) -> list[str]
```

通过在容器内执行 `find` 命令列出目录内容，结果限制 500 条。

#### glob

```python
def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]
```

文件模式匹配，返回 `(匹配列表, 是否截断)` 元组。两种实现路径：

- `include_dirs=False`：使用容器 API 的 `find_files(glob=pattern)`
- `include_dirs=True`：使用 `list_path(recursive=True)` + 本地 `path_matches()` 过滤

#### grep

```python
def grep(self, path: str, pattern: str, *, glob: str | None = None,
         literal: bool = False, case_sensitive: bool = False, max_results: int = 100) -> tuple[list[GrepMatch], bool]
```

容器内正则搜索。先用 `find_files` 或 `list_path` 获取候选文件列表，再逐文件调用 `search_in_file(regex=...)`。

#### update_file

```python
def update_file(self, path: str, content: bytes) -> None
```

写入二进制内容，通过 Base64 编码传输：`base64.b64encode(content).decode("utf-8")`。

## SandboxBackend — 后端抽象

> 文件：`aio_sandbox/backend.py`

```python
class SandboxBackend(ABC):
    def create(self, thread_id, sandbox_id, extra_mounts) -> SandboxInfo
    def destroy(self, info: SandboxInfo) -> None
    def is_alive(self, info: SandboxInfo) -> bool
    def discover(self, sandbox_id: str) -> SandboxInfo | None
    def list_running(self) -> list[SandboxInfo]     # 可选，默认返回空列表
```

### 辅助函数

```python
def wait_for_sandbox_ready(sandbox_url: str, timeout: int = 30) -> bool
```

轮询沙箱健康端点 `GET /v1/sandbox`，直到返回 200 或超时。

## LocalContainerBackend — 本地容器后端

> 文件：`aio_sandbox/local_backend.py`

在本地启动 Docker 或 Apple Container 来运行沙箱容器。

### 运行时检测

```python
def _detect_runtime(self) -> str
```

- **macOS**：优先检测 Apple Container（`container --version`），不可用时回退到 Docker
- **其他平台**：使用 Docker

### 容器启动

```python
def _start_container(self, container_name, port, extra_mounts) -> str
```

启动命令格式：

```bash
docker run --rm -d -p {bind_host}:{port}:8080 --name {name} \
    --security-opt seccomp=unconfined \
    -e KEY=VALUE \
    --mount type=bind,src={host},dst={container},readonly \
    {image}
```

**端口分配**：从 `base_port` 开始搜索空闲端口，最多重试 10 次。端口冲突时自动跳过。

**端口绑定策略**（`_resolve_docker_bind_host`）：

| 场景 | 绑定地址 |
|:-----|:---------|
| 本地开发（默认） | `127.0.0.1` |
| Docker-in-Docker (DooD) | `0.0.0.0`（兼容模式） |
| IPv6 回环 | `[::1]` |
| `DEER_FLOW_SANDBOX_BIND_HOST` 环境变量 | 使用指定地址 |
| `DEER_FLOW_SANDBOX_HOST` 非 localhost | `0.0.0.0` |

### 挂载策略

Docker 使用 `--mount type=bind,...` 语法（避免 Windows 盘符路径与冒号冲突），Apple Container 使用 `-v` 语法。

### 跨进程发现

通过**确定性容器命名**实现跨进程发现：

```
容器名 = {container_prefix}-{sandbox_id}
sandbox_id = sha256(thread_id)[:8]     # 8 字符十六进制
```

任何进程可以通过相同的 `thread_id` 推导出相同的容器名，从而发现并复用已有容器。

### 批量检查

`_batch_inspect()` 使用**单次** `docker inspect` 调用获取所有容器的创建时间和端口映射，避免 N+1 子进程问题。

### 安全选项

Docker 容器添加 `--security-opt seccomp=unconfined`，允许沙箱内执行任意系统调用。

## RemoteSandboxBackend — 远程 Provisioner 后端

> 文件：`aio_sandbox/remote_backend.py`

通过 HTTP API 连接到 Provisioner 服务，由 Provisioner 在 K8s 中动态创建 Pod + NodePort Service。

### 架构

```
┌────────────┐  HTTP   ┌─────────────┐  K8s API  ┌──────────┐
│ Remote     │ ──────► │ Provisioner │ ────────► │   k3s    │
│ Backend    │         │ :8002       │           │ :6443    │
└────────────┘         └─────────────┘           └─────┬────┘
                                                       │ creates
                       ┌─────────────┐           ┌─────▼──────┐
                       │   Backend   │ ────────► │  sandbox   │
                       │             │  direct   │  Pod(s)    │
                       └─────────────┘ k3s:NPort └────────────┘
```

### Provisioner API

| 操作 | HTTP 方法 | 端点 | 说明 |
|:-----|:---------|:-----|:-----|
| 创建沙箱 | `POST` | `/api/sandboxes` | 创建 Pod + NodePort Service |
| 列出沙箱 | `GET` | `/api/sandboxes` | 列出所有运行中的沙箱 |
| 查询沙箱 | `GET` | `/api/sandboxes/{id}` | 获取沙箱状态和 URL |
| 删除沙箱 | `DELETE` | `/api/sandboxes/{id}` | 销毁 Pod + Service |

### 特殊处理

- `create()` 请求体包含 `user_id`（通过 `get_effective_user_id()` 获取）
- `list_running()` 实现了完整的 Provisioner 列表调用，确保进程重启后能发现之前创建的 Pod
- `is_alive()` 检查 Pod 状态是否为 `"Running"`

## AioSandboxProvider — 生命周期管理

> 文件：`aio_sandbox/aio_sandbox_provider.py`

`AioSandboxProvider` 是核心的沙箱管理器，负责容器的创建、获取、释放和销毁。

### 配置加载

```python
def _load_config(self) -> dict
```

从 `config.yaml` 的 `sandbox` 节加载配置：

| 配置键 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `image` | `enterprise-public-cn-beijing.cr.volces.com/.../all-in-one-sandbox:latest` | 容器镜像 |
| `port` | `8080` | 基础端口 |
| `container_prefix` | `"deer-flow-sandbox"` | 容器名前缀 |
| `idle_timeout` | `600`（10 分钟） | 空闲超时（秒） |
| `replicas` | `3` | 最大并发容器数（LRU 淘汰） |
| `mounts` | `[]` | 额外卷挂载配置 |
| `environment` | `{}` | 容器环境变量（`$VAR` 引用环境变量） |
| `provisioner_url` | `""` | Provisioner URL（设置后使用远程后端） |

### 后端选择

```python
def _create_backend(self) -> SandboxBackend:
```

1. `provisioner_url` 已设置 → `RemoteSandboxBackend`（远程 K8s 模式）
2. 默认 → `LocalContainerBackend`（本地 Docker 模式）

### 挂载策略

```python
def _get_extra_mounts(self, thread_id) -> list[tuple[str, str, bool]]
```

为每个沙箱计算挂载列表：

| 宿主路径 | 容器路径 | 读写 | 说明 |
|:---------|:---------|:----:|:-----|
| `{host_sandbox_work_dir}` | `/mnt/user-data/workspace` | RW | 工作目录 |
| `{host_sandbox_uploads_dir}` | `/mnt/user-data/uploads` | RW | 上传文件 |
| `{host_sandbox_outputs_dir}` | `/mnt/user-data/outputs` | RW | 输出文件 |
| `{host_acp_workspace_dir}` | `/mnt/acp-workspace` | RO | ACP 工作区 |
| `{skills_path}` | `/mnt/skills` | RO | 技能目录 |

**路径解析**：
- 宿主路径通过 `get_paths()` 和 `get_effective_user_id()` 计算
- 使用 `host_base_dir`（而非容器内路径），确保 DooD 场景下宿主 Docker daemon 能解析路径
- `DEER_FLOW_HOST_SKILLS_PATH` 环境变量覆盖技能路径（DooD 场景）

### 获取沙箱（acquire）

```python
def acquire(self, thread_id: str | None = None) -> str
```

**三层缓存架构**：

```
Layer 1: 进程内缓存（_thread_sandboxes）
    │ 命中 → 直接返回
    ▼ 未命中
Layer 1.5: 暖池（_warm_pool）
    │ 命中 → 复用运行中的容器
    ▼ 未命中
Layer 2: 后端发现 + 创建（文件锁保护）
    │ discover 命中 → 复用
    │ 未命中 → create 新容器
    ▼
返回 sandbox_id
```

**确定性 ID**：

```python
sandbox_id = sha256(thread_id.encode()).hexdigest()[:8]
```

对于同一个 `thread_id`，所有进程推导出相同的 `sandbox_id`，实现跨进程发现。

**跨进程锁**：使用文件锁（Linux: `fcntl.flock`，Windows: `msvcrt.locking`）防止两个进程同时为同一个 `thread_id` 创建容器。

### 释放沙箱（release）

```python
def release(self, sandbox_id: str) -> None
```

释放不是销毁！容器进入**暖池**（warm pool），保持运行状态：

- 同一 `thread_id` 下次 `acquire` 可直接从暖池复用，无需冷启动
- 暖池中的容器在 `idle_timeout` 后由空闲检查器销毁
- 或在 `replicas` 容量不足时被 LRU 淘汰

### 副本管理（replicas）

- 默认最大 **3 个**并发沙箱容器
- 超过限制时，淘汰暖池中**最旧**的容器（`_evict_oldest_warm`）
- 活跃容器不会被强制停止（软限制）
- 超限时仍然创建，但记录警告日志

### 空闲检查

后台守护线程每 **60 秒**检查一次：

1. **活跃沙箱**：`当前时间 - last_activity > idle_timeout` → 销毁
2. **暖池沙箱**：`当前时间 - release_timestamp > idle_timeout` → 销毁
3. 销毁前**重新验证**空闲状态（防止在检查间隔中被重新获取）

### 启动协调

```python
def _reconcile_orphans(self) -> None
```

进程启动时扫描所有运行中的容器，无条件收养到暖池。解决以下场景：

- 进程重启后内存状态丢失，但 Docker 容器仍在运行
- 进程崩溃（SIGKILL）后容器成为孤儿
- 新进程接管旧容器，由空闲检查器决定是否保留

### 信号处理

注册 SIGTERM、SIGINT、SIGHUP 处理器，确保：
- 用户关闭终端时容器被清理
- 进程被 kill 时执行 `shutdown()`
- 信号处理后调用原始处理器（链式处理）

### 优雅关闭

```python
def shutdown(self) -> None
```

1. 标记 `_shutdown_called = True`（幂等）
2. 停止空闲检查线程
3. 销毁所有活跃沙箱
4. 销毁暖池中的所有沙箱

## 与 LocalSandbox 的对比

| 特性 | LocalSandbox | AioSandbox |
|:-----|:-------------|:-----------|
| **执行环境** | 宿主机文件系统 | Docker 容器 |
| **安全隔离** | 无（完全信任） | 容器级隔离 |
| **网络隔离** | 无 | 容器网络命名空间 |
| **文件系统** | 路径映射（虚拟路径翻译） | bind-mount + 容器内路径 |
| **并发** | 无限制 | 受 `replicas` 限制 |
| **冷启动** | 无（即时） | 3-10 秒（容器启动） |
| **资源开销** | 极低 | 每容器占用内存和 CPU |
| **跨进程发现** | 不需要 | 通过确定性 ID + 文件锁 |
| **适用场景** | 开发、测试、受信环境 | 生产、多租户、不信任代码 |

## 配置示例

```yaml
# config.yaml — 本地 Docker 模式
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
  port: 8080
  container_prefix: deer-flow-sandbox
  idle_timeout: 600
  replicas: 3
  mounts:
    - host_path: /data/shared
      container_path: /mnt/shared
      read_only: true
  environment:
    NODE_ENV: production
    API_KEY: $MY_API_KEY
```

```yaml
# config.yaml — 远程 K8s Provisioner 模式
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  provisioner_url: http://provisioner:8002
  idle_timeout: 600
  replicas: 5
```
