"""链路追踪回调工厂 —— 根据配置动态构建追踪回调处理器。

本模块实现了 DeerFlow 追踪系统的工厂模式入口。根据应用配置中声明的
追踪提供者列表，动态创建对应的 LangChain 回调处理器实例。

架构设计：
    - **工厂模式** —— ``build_tracing_callbacks`` 是唯一的公开入口，
      内部根据 provider 名称分发到各自的私有创建函数。
    - **延迟导入** —— LangSmith 和 Langfuse 的 SDK 仅在实际启用时
      才被导入，避免在未安装相关包时触发 ImportError。
    - **快速失败** —— 初始化失败时立即抛出 ``RuntimeError``，
      而非静默跳过，确保运维人员在部署时就能发现配置问题。

支持的追踪提供者：
    - **LangSmith**（``"langsmith"``）
      使用 ``LangChainTracer`` 回调，通过 ``project_name`` 参数
      将追踪数据组织到指定项目中。适合使用 LangChain 官方云服务的团队。

    - **Langfuse**（``"langfuse"``）
      使用 ``LangfuseCallbackHandler`` 回调，支持自托管的
      开源可观测性平台。Langfuse >= 4 版本通过客户端单例初始化
      项目级凭证，回调处理器自动关联到已配置的客户端。

配置读取流程：
    1. ``validate_enabled_tracing_providers`` 校验声明的提供者是否有效。
    2. ``get_enabled_tracing_providers`` 获取已启用的提供者列表。
    3. ``get_tracing_config`` 获取全局追踪配置（含各提供者的连接参数）。

典型用法::

    from deerflow.tracing.factory import build_tracing_callbacks

    callbacks = build_tracing_callbacks()
    # callbacks 为空列表时表示未启用任何追踪
    result = agent.invoke(input, config={"callbacks": callbacks})
"""

from __future__ import annotations

from typing import Any

from deerflow.config import (
    get_enabled_tracing_providers,
    get_tracing_config,
    validate_enabled_tracing_providers,
)


def _create_langsmith_tracer(config) -> Any:
    """创建 LangSmith 追踪回调处理器。

    使用 LangChain 官方的 ``LangChainTracer``，将 LLM 调用追踪数据
    发送到 LangSmith 云平台。追踪数据按项目（project）组织，便于
    在 LangSmith UI 中进行筛选和分析。

    Args:
        config: LangSmith 配置对象，需包含 ``project`` 字段。

    Returns:
        ``LangChainTracer`` 实例。

    Note:
        此函数采用延迟导入（函数内 import），仅在实际启用 LangSmith
        时才会触发 import，避免在未安装 ``langchain-core`` 时报错。
    """
    from langchain_core.tracers.langchain import LangChainTracer

    return LangChainTracer(project_name=config.project)


def _create_langfuse_handler(config) -> Any:
    """创建 Langfuse 追踪回调处理器。

    Langfuse >= 4 版本采用客户端单例模式：先通过 ``Langfuse()``
    初始化项目级凭证（secret_key、public_key、host），SDK 内部会
    将此客户端注册为全局单例；随后创建的 ``LangfuseCallbackHandler``
    会自动关联到该已配置的客户端。

    这种设计的优势是：回调处理器无需重复传递凭证，且多个回调实例
    共享同一连接池和缓冲区，减少资源开销。

    Args:
        config: Langfuse 配置对象，需包含以下字段：
            - ``secret_key`` (str): API 密钥
            - ``public_key`` (str): 公钥
            - ``host`` (str): Langfuse 服务地址

    Returns:
        ``LangfuseCallbackHandler`` 实例。

    Raises:
        ImportError: 未安装 ``langfuse`` 包时抛出。

    Note:
        此函数采用延迟导入（函数内 import），仅在实际启用 Langfuse
        时才会触发 import。
    """
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    # langfuse>=4 通过客户端单例初始化项目级凭证；
    # LangChain 回调处理器随后关联到该已配置的客户端实例
    Langfuse(
        secret_key=config.secret_key,
        public_key=config.public_key,
        host=config.host,
    )
    return LangfuseCallbackHandler(public_key=config.public_key)


def build_tracing_callbacks() -> list[Any]:
    """构建所有已启用追踪提供者的回调处理器列表。

    这是追踪模块的公开入口函数。执行流程：
    1. 校验配置中声明的追踪提供者是否合法。
    2. 读取已启用的提供者列表。
    3. 为每个提供者创建对应的回调处理器。
    4. 收集所有回调到一个列表中返回。

    调用者可直接将返回的列表传递给 LangChain 的 ``config["callbacks"]``
    参数，LangChain 会在每次 LLM 调用时自动触发所有回调。

    Returns:
        回调处理器列表。如果未启用任何追踪提供者，返回空列表 ``[]``。
        列表中的元素类型取决于启用的提供者：
        - LangSmith → ``LangChainTracer``
        - Langfuse → ``LangfuseCallbackHandler``

    Raises:
        RuntimeError: 任何追踪提供者的初始化失败时抛出，
            包含原始异常信息和提供者名称。

    Example::

        callbacks = build_tracing_callbacks()
        # 将回调注入到 LangChain Agent 的执行配置中
        result = agent.invoke(
            {"messages": [("user", "Hello")]},
            config={"callbacks": callbacks},
        )
    """
    # 先校验配置中声明的提供者名称是否在支持范围内
    validate_enabled_tracing_providers()
    enabled_providers = get_enabled_tracing_providers()
    # 未启用任何提供者时快速返回，避免不必要的配置读取
    if not enabled_providers:
        return []

    tracing_config = get_tracing_config()
    callbacks: list[Any] = []

    for provider in enabled_providers:
        if provider == "langsmith":
            try:
                callbacks.append(_create_langsmith_tracer(tracing_config.langsmith))
            except Exception as exc:  # pragma: no cover - exercised via tests with monkeypatch
                # 快速失败策略：追踪初始化出错时不静默跳过，
                # 而是立即抛出异常，确保运维人员在部署时就能发现配置问题
                raise RuntimeError(f"LangSmith tracing initialization failed: {exc}") from exc
        elif provider == "langfuse":
            try:
                callbacks.append(_create_langfuse_handler(tracing_config.langfuse))
            except Exception as exc:  # pragma: no cover - exercised via tests with monkeypatch
                raise RuntimeError(f"Langfuse tracing initialization failed: {exc}") from exc

    return callbacks
