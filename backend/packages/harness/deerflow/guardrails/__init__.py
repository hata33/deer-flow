"""工具调用前置授权（Guardrails）模块。

Guardrails 是 DeerFlow 的安全护栏层，在每次工具调用执行前进行策略评估，
决定是否允许该调用继续。它位于中间件链的第 5 位，在沙箱初始化之后、工具实际执行之前。

核心设计理念：
- 确定性授权：基于策略的自动化判定，无需人工介入
- 可插拔 Provider：通过 Protocol 定义接口，支持内置白名单、OAP 护照、自定义三种 Provider
- 安全优先（fail-closed）：Provider 异常时默认阻止调用，宁可误杀不可放过
- Agent 可自愈：拒绝时返回 ToolMessage（status=error），Agent 看到错误后可选择替代方案

模块架构：
- provider.py  —— 数据类型（Request/Decision/Reason）和 Provider 协议
- middleware.py —— AgentMiddleware 实现，拦截 wrap_tool_call/awrap_tool_call
- builtin.py   —— 内置 AllowlistProvider，零外部依赖
- __init__.py   —— 模块入口，公开接口导出

上游消费者：
- agents/middlewares/tool_error_handling_middleware.py → 根据 guardrails_config 注册 GuardrailMiddleware
- agents/factory.py → 通过 RuntimeFeatures.guardrail 控制开关
- config/guardrails_config.py → 配置模型和单例管理
"""

from deerflow.guardrails.builtin import AllowlistProvider
from deerflow.guardrails.middleware import GuardrailMiddleware
from deerflow.guardrails.provider import GuardrailDecision, GuardrailProvider, GuardrailReason, GuardrailRequest

__all__ = [
    "AllowlistProvider",
    "GuardrailDecision",
    "GuardrailMiddleware",
    "GuardrailProvider",
    "GuardrailReason",
    "GuardrailRequest",
]
