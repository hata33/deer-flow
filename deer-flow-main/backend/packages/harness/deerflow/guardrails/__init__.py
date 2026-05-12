"""工具调用前置授权（Guardrails）模块。

在工具执行前进行策略评估，拒绝不合规的工具调用。
支持可插拔的 GuardrailProvider 协议和内置的 AllowlistProvider。

组件：
- provider: GuardrailProvider 协议和数据结构（请求、决策、原因）
- middleware: GuardrailMiddleware，在中间件链中拦截工具调用
- builtin: 内置的 AllowlistProvider（零依赖的允许/拒绝列表）
"""

from deerflow.guardrails.builtin import AllowlistProvider
from deerflow.guardrails.middleware import GuardrailMiddleware
from deerflow.guardrails.provider import GuardrailDecision, GuardrailProvider, GuardrailReason, GuardrailRequest

__all__ = [
    "AllowlistProvider",      # 内置允许/拒绝列表 provider
    "GuardrailDecision",      # 授权决策（allow/deny + 原因）
    "GuardrailMiddleware",    # 工具调用拦截中间件
    "GuardrailProvider",      # 授权 provider 协议
    "GuardrailReason",        # 决策原因（OAP 格式）
    "GuardrailRequest",       # 授权请求上下文
]
