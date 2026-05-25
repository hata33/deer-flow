"""安全门控模块 —— 为本地沙箱（LocalSandbox）提供能力限制。

本模块实现了沙箱级别的安全策略，主要用于在 LocalSandboxProvider 下限制
某些不安全的操作。核心设计思想是：

**本地沙箱并非真正的隔离边界**。与 Docker 容器（AioSandboxProvider）不同，
LocalSandbox 直接在宿主机文件系统上操作，Agent 执行的 bash 命令可以访问
宿主机上的任意资源。因此，在本地沙箱模式下，需要额外的安全门控。

主要限制
~~~~~~~~
1. **Host Bash 执行限制**：默认禁止 Agent 通过 LocalSandbox 执行 bash 命令，
   因为这些命令直接运行在宿主机上，没有隔离保护。
   - 可通过配置 ``sandbox.allow_host_bash: true`` 显式允许（仅在完全可信环境中）
   - 使用 AioSandboxProvider 则无此限制（命令运行在容器内）

2. **Bash Subagent 限制**：基于同样的安全考虑，禁止在本地沙箱模式下启动
   Bash Subagent（一个专门执行 shell 命令的子 Agent）。

检测机制
~~~~~~~~
通过检查配置中的 ``sandbox.use`` 字段来判断当前是否使用本地沙箱：

- :func:`uses_local_sandbox_provider` — 判断当前是否使用 LocalSandboxProvider
- :func:`is_host_bash_allowed` — 判断是否允许在本地沙箱中执行 bash 命令

配置示例
~~~~~~~~
在 ``config.yaml`` 中::

    sandbox:
      use: "deerflow.sandbox.local:LocalSandboxProvider"
      allow_host_bash: true  # 仅在可信环境中启用
"""

from deerflow.config import get_app_config

# LocalSandboxProvider 的已知类路径标识符，用于匹配配置中的 sandbox.use 值。
# 这些标识符用于精确匹配或模式匹配来判断当前是否使用本地沙箱。
_LOCAL_SANDBOX_PROVIDER_MARKERS = (
    "deerflow.sandbox.local:LocalSandboxProvider",
    "deerflow.sandbox.local.local_sandbox_provider:LocalSandboxProvider",
)

# 当 LocalSandboxProvider 下尝试执行 bash 命令但被禁止时显示的错误消息。
# 引导用户切换到 AioSandboxProvider（提供容器隔离）或在可信环境中显式启用。
LOCAL_HOST_BASH_DISABLED_MESSAGE = (
    "Host bash execution is disabled for LocalSandboxProvider because it is not a secure "
    "sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or set "
    "sandbox.allow_host_bash: true only in a fully trusted local environment."
)

# 当 LocalSandboxProvider 下尝试启动 Bash Subagent 但被禁止时显示的错误消息。
LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE = (
    "Bash subagent is disabled for LocalSandboxProvider because host bash execution is not "
    "a secure sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or "
    "set sandbox.allow_host_bash: true only in a fully trusted local environment."
)


def uses_local_sandbox_provider(config=None) -> bool:
    """判断当前沙箱提供者是否为 LocalSandboxProvider。

    通过检查配置中的 ``sandbox.use`` 字段来判定。支持两种匹配方式：
    1. 精确匹配预定义的标记字符串
    2. 模式匹配：以 ``:LocalSandboxProvider`` 结尾且包含 ``deerflow.sandbox.local``

    Args:
        config: 应用配置对象。如果为 None，则自动获取当前配置。

    Returns:
        如果使用 LocalSandboxProvider 则返回 True，否则返回 False。
    """
    if config is None:
        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    sandbox_use = getattr(sandbox_cfg, "use", "")
    # 精确匹配已知标记
    if sandbox_use in _LOCAL_SANDBOX_PROVIDER_MARKERS:
        return True
    # 模式匹配：兼容其他可能的模块路径写法
    return sandbox_use.endswith(":LocalSandboxProvider") and "deerflow.sandbox.local" in sandbox_use


def is_host_bash_allowed(config=None) -> bool:
    """判断是否允许在本地沙箱中执行宿主机 bash 命令。

    逻辑如下：
    - 如果使用的不是 LocalSandboxProvider（如 AioSandboxProvider），始终允许
    - 如果使用的是 LocalSandboxProvider，需要配置中显式设置
      ``sandbox.allow_host_bash: true`` 才允许
    - 默认情况下（未配置）不允许

    Args:
        config: 应用配置对象。如果为 None，则自动获取当前配置。

    Returns:
        如果允许执行 bash 命令则返回 True，否则返回 False。
    """
    if config is None:
        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    if sandbox_cfg is None:
        # 没有沙箱配置，默认不允许
        return False
    # 非 LocalSandboxProvider（如 Docker 沙箱）始终允许 bash
    if not uses_local_sandbox_provider(config):
        return True
    # LocalSandboxProvider 下需要显式配置允许
    return bool(getattr(sandbox_cfg, "allow_host_bash", False))
