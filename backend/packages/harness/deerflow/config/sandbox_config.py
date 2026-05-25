"""沙箱配置 — 代码执行环境的安全隔离。

沙箱系统为 Agent 的工具执行提供隔离环境。本配置声明：
- 使用哪种沙箱 Provider（本地文件系统 或 Docker 容器）
- Docker 容器的参数（镜像、端口、副本数、卷挂载）
- 工具输出的截断限制（防止超长输出耗尽上下文）

### LocalSandboxProvider
直接在宿主机文件系统上执行，通过虚拟路径映射实现隔离。
适用于本地开发和可信环境。

### AioSandboxProvider
使用 Docker 容器隔离执行，每个线程分配独立容器。
适用于生产环境和不可信代码执行。

### 输出截断
bash、read_file、ls 三个工具的输出可能非常长（如日志文件）。
截断策略：
- bash: 中间截断（保留头部和尾部各一半），因为命令输出开头和结尾通常最有用
- read_file: 头部截断，因为文件通常开头是关键内容
- ls: 头部截断
"""

from pydantic import BaseModel, ConfigDict, Field


class VolumeMountConfig(BaseModel):
    """Docker 卷挂载配置。

    将宿主机目录映射到容器内路径，用于：
    - 共享技能目录
    - 挂载配置文件
    - 共享缓存目录
    """

    host_path: str = Field(..., description="Path on the host machine")
    container_path: str = Field(..., description="Path inside the container")
    read_only: bool = Field(default=False, description="Whether the mount is read-only")


class SandboxConfig(BaseModel):
    """沙箱系统配置。

    ### 通用字段
    - use: 沙箱 Provider 类路径（必需）
    - allow_host_bash: 允许本地沙箱直接在宿主机执行 bash（危险！）

    ### AioSandboxProvider 专用
    - image: Docker 镜像
    - port: 容器基础端口
    - replicas: 最大并发容器数（超出时 LRU 淘汰最久未使用的）
    - idle_timeout: 空闲超时（秒），0 表示不超时
    - mounts: 额外卷挂载
    - environment: 注入到容器的环境变量

    ### 输出截断
    - bash_output_max_chars: bash 工具输出最大字符数
    - read_file_output_max_chars: read_file 输出最大字符数
    - ls_output_max_chars: ls 输出最大字符数

    extra="allow" 允许 Provider 特定的额外字段透传。
    """

    use: str = Field(
        ...,
        description="Class path of the sandbox provider (e.g. deerflow.sandbox.local:LocalSandboxProvider)",
    )
    allow_host_bash: bool = Field(
        default=False,
        description="Allow the bash tool to execute directly on the host when using LocalSandboxProvider. Dangerous; intended only for fully trusted local environments.",
    )
    image: str | None = Field(
        default=None,
        description="Docker image to use for the sandbox container",
    )
    port: int | None = Field(
        default=None,
        description="Base port for sandbox containers",
    )
    replicas: int | None = Field(
        default=None,
        description="Maximum number of concurrent sandbox containers (default: 3). When the limit is reached the least-recently-used sandbox is evicted to make room.",
    )
    container_prefix: str | None = Field(
        default=None,
        description="Prefix for container names",
    )
    idle_timeout: int | None = Field(
        default=None,
        description="Idle timeout in seconds before sandbox is released (default: 600 = 10 minutes). Set to 0 to disable.",
    )
    mounts: list[VolumeMountConfig] = Field(
        default_factory=list,
        description="List of volume mounts to share directories between host and container",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to inject into the sandbox container. Values starting with $ will be resolved from host environment variables.",
    )

    bash_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from bash tool output. Output exceeding this limit is middle-truncated (head + tail), preserving the first and last half. Set to 0 to disable truncation.",
    )
    read_file_output_max_chars: int = Field(
        default=50000,
        ge=0,
        description="Maximum characters to keep from read_file tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )
    ls_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from ls tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )

    model_config = ConfigDict(extra="allow")
