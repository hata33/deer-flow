"""沙箱配置定义。

本模块定义了 DeerFlow 沙箱执行环境的配置结构。
沙箱为代理的工具执行（bash、文件读写等）提供隔离的运行环境。

支持的沙箱类型：
    - **LocalSandboxProvider** — 本地文件系统沙箱（无隔离）。
        适合开发测试环境，allow_host_bash 可启用宿主机 bash 执行（危险）。
    - **AioSandboxProvider** — Docker 容器沙箱（完整隔离）。
        每个线程一个独立的 Docker 容器，支持卷挂载和环境变量注入。

Docker 沙箱关键参数：
    - **image** — Docker 镜像地址
    - **replicas** — 最大并发容器数（默认 3），达上限时淘汰最近最少使用的容器
    - **idle_timeout** — 空闲超时（默认 600 秒 = 10 分钟），设为 0 禁用

配置示例（config.yaml）：
    ```yaml
    sandbox:
      use: deerflow.sandbox.local:LocalSandboxProvider
      allow_host_bash: false

    # Docker 沙箱
    sandbox:
      use: packages.harness.deerflow.community.aio_sandbox:AioSandboxProvider
      image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest
      replicas: 3
      idle_timeout: 600
      mounts:
        - host_path: /data/shared
          container_path: /mnt/shared
          read_only: true
      environment:
        API_KEY: $MY_API_KEY
    ```

注意：
    - model_config = ConfigDict(extra="allow") 允许传入额外的提供商特定参数。
    - environment 中以 $ 开头的值会从宿主机环境变量解析。
"""
from pydantic import BaseModel, ConfigDict, Field


class VolumeMountConfig(BaseModel):
    """Docker 卷挂载配置。

    用于将宿主机目录共享到沙箱容器中。

    Attributes:
        host_path: 宿主机上的路径。
        container_path: 容器内的挂载路径。
        read_only: 是否只读挂载。
    """

    host_path: str = Field(..., description="宿主机上的路径")
    container_path: str = Field(..., description="容器内的挂载路径")
    read_only: bool = Field(default=False, description="是否只读挂载")


class SandboxConfig(BaseModel):
    """沙箱配置。

    通用选项：
        use: 沙箱提供者的类路径（必填）
        allow_host_bash: 为 LocalSandboxProvider 启用宿主机 bash 执行。
            危险选项，仅适用于完全可信的本地工作流。

    AioSandboxProvider 特定选项：
        image: Docker 镜像
        port: 沙箱容器的基础端口
        replicas: 最大并发容器数（默认 3），达上限时淘汰最近最少使用的容器
        container_prefix: 容器名前缀
        idle_timeout: 空闲超时秒数（默认 600 = 10 分钟），设为 0 禁用
        mounts: 与容器共享目录的卷挂载列表
        environment: 注入到容器的环境变量（$ 前缀从宿主机环境解析）

    Attributes:
        use: 沙箱提供者类路径（如 deerflow.sandbox.local:LocalSandboxProvider）。
        allow_host_bash: 是否允许 bash 工具在宿主机直接执行。
        image: Docker 镜像地址。
        port: 沙箱容器基础端口。
        replicas: 最大并发容器数。
        container_prefix: 容器名前缀。
        idle_timeout: 空闲超时（秒）。
        mounts: 卷挂载配置列表。
        environment: 注入到容器的环境变量。
    """

    use: str = Field(
        ...,
        description="沙箱提供者类路径（如 deerflow.sandbox.local:LocalSandboxProvider）",
    )
    allow_host_bash: bool = Field(
        default=False,
        description="允许 bash 工具在使用 LocalSandboxProvider 时直接在宿主机执行（危险；仅适用于完全可信的本地环境）",
    )
    image: str | None = Field(
        default=None,
        description="沙箱容器的 Docker 镜像",
    )
    port: int | None = Field(
        default=None,
        description="沙箱容器基础端口",
    )
    replicas: int | None = Field(
        default=None,
        description="最大并发容器数（默认 3）。达上限时淘汰最近最少使用的容器。",
    )
    container_prefix: str | None = Field(
        default=None,
        description="容器名前缀",
    )
    idle_timeout: int | None = Field(
        default=None,
        description="空闲超时（秒），超时后释放沙箱（默认 600 = 10 分钟）。设为 0 禁用。",
    )
    mounts: list[VolumeMountConfig] = Field(
        default_factory=list,
        description="卷挂载列表，用于在宿主机和容器间共享目录",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="注入到沙箱容器的环境变量。以 $ 开头的值从宿主机环境变量解析。",
    )

    # 允许传入额外的提供商特定参数
    model_config = ConfigDict(extra="allow")
