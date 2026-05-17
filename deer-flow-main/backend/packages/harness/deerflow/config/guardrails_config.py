"""工具调用前置授权（Guardrails）配置。

本模块定义了 DeerFlow 的 Guardrails 系统——在工具执行前进行授权检查。

工作原理：
    当 Guardrails 启用时，每个工具调用都会经过配置的 Provider 进行审批：
    1. Provider 接收工具名称、参数和代理的 passport 引用。
    2. Provider 返回 allow/deny 决定。
    3. deny 的调用会被替换为错误 ToolMessage，阻止实际执行。

可用 Provider 类型：
    - **AllowlistProvider**（内置）— 基于允许列表的简单授权，零外部依赖。
    - **OAP 策略 Provider**（如 ``aport-agent-guardrails``）— 基于 OAP 策略的授权。
    - **自定义 Provider** — 实现 GuardrailProvider 协议的任意 Provider。

fail_closed 模式：
    当 Provider 发生错误时的行为：
    - True（默认）— 阻止工具调用（安全优先）。
    - False — 放行工具调用（可用性优先）。

配置示例（config.yaml）：
    ```yaml
    guardrails:
      enabled: true
      fail_closed: true
      passport: /path/to/passport.json
      provider:
        use: deerflow.guardrails.builtin:AllowlistProvider
        config:
          allowed_tools:
            - bash
            - read_file
            - write_file
    ```
"""
from pydantic import BaseModel, Field


class GuardrailProviderConfig(BaseModel):
    """Guardrail Provider 的配置。

    Attributes:
        use: Provider 类的完整路径（如 ``deerflow.guardrails.builtin:AllowlistProvider``）。
            通过反射系统（resolve_class）动态加载。
        config: Provider 特定的配置参数，作为 kwargs 传递给 Provider 构造函数。
    """

    use: str = Field(description="Provider 类路径（如 'deerflow.guardrails.builtin:AllowlistProvider'）")
    config: dict = Field(default_factory=dict, description="Provider 特定的配置参数")


class GuardrailsConfig(BaseModel):
    """工具调用前置授权配置。

    Attributes:
        enabled: 是否启用 Guardrail 中间件。
        fail_closed: Provider 出错时是否阻止工具调用（安全优先）。
        passport: OAP passport 路径或托管代理 ID。
        provider: Guardrail Provider 配置。
    """

    enabled: bool = Field(default=False, description="是否启用 Guardrail 中间件")
    fail_closed: bool = Field(default=True, description="Provider 出错时是否阻止工具调用")
    passport: str | None = Field(default=None, description="OAP passport 路径或托管代理 ID")
    provider: GuardrailProviderConfig | None = Field(default=None, description="Guardrail Provider 配置")


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_guardrails_config: GuardrailsConfig | None = None


def get_guardrails_config() -> GuardrailsConfig:
    """获取 Guardrails 配置。

    如果尚未通过 load_guardrails_config_from_dict() 加载，
    返回默认配置（enabled=False）。
    """
    global _guardrails_config
    if _guardrails_config is None:
        _guardrails_config = GuardrailsConfig()
    return _guardrails_config


def load_guardrails_config_from_dict(data: dict) -> GuardrailsConfig:
    """从字典加载 Guardrails 配置（由 AppConfig.from_file 调用）。

    Args:
        data: config.yaml 中 guardrails 字段的字典。

    Returns:
        加载后的 GuardrailsConfig 实例。
    """
    global _guardrails_config
    _guardrails_config = GuardrailsConfig.model_validate(data)
    return _guardrails_config


def reset_guardrails_config() -> None:
    """重置缓存的 Guardrails 配置实例。

    主要用于测试，防止单例泄漏导致测试间相互影响。
    """
    global _guardrails_config
    _guardrails_config = None
