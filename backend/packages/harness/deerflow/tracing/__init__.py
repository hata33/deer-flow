"""链路追踪（Tracing）模块 —— LLM 可观测性回调构建。

本模块为 DeerFlow 系统提供统一的 LLM 调用链路追踪能力。通过配置文件
中声明的追踪提供者（Tracing Provider），自动构建对应的 LangChain
回调处理器（Callback Handler），用于记录每次 LLM 调用的输入、输出、
延迟和 token 消耗等指标。

支持的追踪提供者：
    - **LangSmith** —— LangChain 官方的可观测性平台，
      通过 ``LangChainTracer`` 回调接入，支持项目级别的追踪组织。
    - **Langfuse** —— 开源的 LLM 可观测性平台，
      通过 ``LangfuseCallbackHandler`` 回调接入，支持细粒度的
      prompt 版本管理和成本追踪。

架构定位：
    本模块是追踪功能的工厂层（Factory），仅负责根据配置创建回调实例。
    具体的回调注入和生命周期管理由上层调用者（如 Agent 执行引擎）完成。

配置驱动：
    启用哪些追踪提供者、各自的连接参数（API Key、Host 等）均在
    DeerFlow 应用配置文件中声明，本模块在运行时读取配置并动态创建实例。

模块导出：
    - :func:`build_tracing_callbacks` —— 构建所有已启用追踪提供者的回调列表

典型用法::

    from deerflow.tracing import build_tracing_callbacks

    callbacks = build_tracing_callbacks()
    # 将 callbacks 传递给 LangChain Agent 的 execute 方法
    result = agent.invoke(input, config={"callbacks": callbacks})
"""

from .factory import build_tracing_callbacks

__all__ = ["build_tracing_callbacks"]
