"""Guardrails 配置 — 工具调用前置授权。

Guardrails 系统在每次工具执行前进行安全检查，决定是否允许该工具调用。
这为 Agent 的自主工具使用提供了安全保障。

### 工作原理
1. Agent 产生工具调用意图
2. GuardrailMiddleware 拦截，调用配置的 Provider
3. Provider 根据工具名称、参数和上下文做出 allow/deny 决策
4. deny 时返回错误 ToolMessage，Agent 需要调整策略

### Provider 类型
- AllowlistProvider（内置）: 基于白名单过滤，零依赖
- OAP Policy Provider: 使用 aport-agent-guardrails 等外部策略引擎
- 自定义 Provider: 实现GuardrailProvider 协议

### fail_closed 策略
当 Provider 出错时：
- fail_closed=true（默认）: 阻止工具调用（安全优先）
- fail_closed=false: 放行工具调用（可用性优先）

本配置作为全局单例管理。
"""

from pydantic import BaseModel, Field


class GuardrailProviderConfig(BaseModel):
    """Guardrail Provider 配置。

    - use: Provider 类路径（如 deerflow.guardrails.builtin:AllowlistProvider）
    - config: 传递给 Provider 构造函数的额外参数
    """

    use: str = Field(description="Class path (e.g. 'deerflow.guardrails.builtin:AllowlistProvider')")
    config: dict = Field(default_factory=dict, description="Provider-specific settings passed as kwargs")


class GuardrailsConfig(BaseModel):
    """Guardrails 全局配置。

    - enabled: 是否启用 Guardrail 中间件
    - fail_closed: Provider 出错时是否阻止工具调用
    - passport: OAP 护照路径或托管的 Agent ID
    - provider: 使用的 Guardrail Provider 配置
    """

    enabled: bool = Field(default=False, description="Enable guardrail middleware")
    fail_closed: bool = Field(default=True, description="Block tool calls if provider errors")
    passport: str | None = Field(default=None, description="OAP passport path or hosted agent ID")
    provider: GuardrailProviderConfig | None = Field(default=None, description="Guardrail provider configuration")


# 全局单例 — 由 AppConfig._apply_singleton_configs() 更新
_guardrails_config: GuardrailsConfig | None = None


def get_guardrails_config() -> GuardrailsConfig:
    """获取当前 Guardrails 配置。

    未加载时返回默认配置（enabled=False）。
    """
    global _guardrails_config
    if _guardrails_config is None:
        _guardrails_config = GuardrailsConfig()
    return _guardrails_config


def load_guardrails_config_from_dict(data: dict) -> GuardrailsConfig:
    """从字典加载 Guardrails 配置（由 AppConfig 初始化时调用）。"""
    global _guardrails_config
    _guardrails_config = GuardrailsConfig.model_validate(data)
    return _guardrails_config


def reset_guardrails_config() -> None:
    """重置缓存的配置实例。用于测试，防止单例泄漏。"""
    global _guardrails_config
    _guardrails_config = None
