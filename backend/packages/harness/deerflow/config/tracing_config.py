"""追踪配置 — LangSmith 和 Langfuse 可观测性。

追踪系统将 Agent 的执行过程（工具调用、LLM 交互、中间结果）发送到
可观测性平台，用于调试、性能分析和质量监控。

### 支持的 Provider
1. **LangSmith**: LangChain 生态的可观测性平台
2. **Langfuse**: 开源的 LLM 可观测性平台

### 配置来源
所有配置从环境变量读取（不从 config.yaml）：
- LangSmith: LANGSMITH_TRACING / LANGCHAIN_TRACING_V2, LANGSMITH_API_KEY / LANGCHAIN_API_KEY
- Langfuse: LANGFUSE_TRACING, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

### 为什么用环境变量而非 config.yaml
- 追踪是运维关注点，不应混入应用配置
- 环境变量在 CI/CD 和容器编排中更易管理
- LangSmith/Langfuse 的 SDK 本身就读环境变量

### 双重检查
- enabled: 用户显式启用了追踪（即使配置不完整）
- is_configured: enabled 且凭证完整，可以实际工作
只检查 is_configured 时可能会遗漏配置不完整的情况，
所以 validate_enabled 专门验证"启用了但缺少凭证"的场景。
"""

import os
import threading

from pydantic import BaseModel, Field

_config_lock = threading.Lock()


class LangSmithTracingConfig(BaseModel):
    """LangSmith 追踪配置。

    环境变量映射（优先使用 LANGSMITH_* 变量，回退到 LANGCHAIN_* 变量）：
    - LANGSMITH_TRACING / LANGCHAIN_TRACING_V2 / LANGCHAIN_TRACING: 是否启用
    - LANGSMITH_API_KEY / LANGCHAIN_API_KEY: API 密钥
    - LANGSMITH_PROJECT / LANGCHAIN_PROJECT: 项目名
    - LANGSMITH_ENDPOINT / LANGCHAIN_ENDPOINT: API 端点
    """

    enabled: bool = Field(...)
    api_key: str | None = Field(...)
    project: str = Field(...)
    endpoint: str = Field(...)

    @property
    def is_configured(self) -> bool:
        """enabled 且有 API 密钥 → 可以实际工作。"""
        return self.enabled and bool(self.api_key)

    def validate(self) -> None:
        """验证启用了追踪但缺少 API 密钥时抛出错误。"""
        if self.enabled and not self.api_key:
            raise ValueError("LangSmith tracing is enabled but LANGSMITH_API_KEY (or LANGCHAIN_API_KEY) is not set.")


class LangfuseTracingConfig(BaseModel):
    """Langfuse 追踪配置。

    环境变量映射：
    - LANGFUSE_TRACING: 是否启用
    - LANGFUSE_PUBLIC_KEY: 公钥
    - LANGFUSE_SECRET_KEY: 密钥
    - LANGFUSE_BASE_URL: 自定义 URL（默认 https://cloud.langfuse.com）
    """

    enabled: bool = Field(...)
    public_key: str | None = Field(...)
    secret_key: str | None = Field(...)
    host: str = Field(...)

    @property
    def is_configured(self) -> bool:
        """enabled 且有公钥和密钥 → 可以实际工作。"""
        return self.enabled and bool(self.public_key) and bool(self.secret_key)

    def validate(self) -> None:
        """验证启用了追踪但缺少凭证时抛出错误。"""
        if not self.enabled:
            return
        missing: list[str] = []
        if not self.public_key:
            missing.append("LANGFUSE_PUBLIC_KEY")
        if not self.secret_key:
            missing.append("LANGFUSE_SECRET_KEY")
        if missing:
            raise ValueError(f"Langfuse tracing is enabled but required settings are missing: {', '.join(missing)}")


class TracingConfig(BaseModel):
    """追踪系统总配置。

    聚合 LangSmith 和 Langfuse 两个 Provider 的配置。
    """

    langsmith: LangSmithTracingConfig = Field(...)
    langfuse: LangfuseTracingConfig = Field(...)

    @property
    def is_configured(self) -> bool:
        """至少有一个 Provider 配置完整且启用。"""
        return bool(self.enabled_providers)

    @property
    def explicitly_enabled_providers(self) -> list[str]:
        """用户显式启用了的 Provider（即使配置不完整）。

        用于验证场景：发现"启用了但配不全"的情况。
        """
        enabled: list[str] = []
        if self.langsmith.enabled:
            enabled.append("langsmith")
        if self.langfuse.enabled:
            enabled.append("langfuse")
        return enabled

    @property
    def enabled_providers(self) -> list[str]:
        """配置完整且启用的 Provider（可以实际工作的）。"""
        enabled: list[str] = []
        if self.langsmith.is_configured:
            enabled.append("langsmith")
        if self.langfuse.is_configured:
            enabled.append("langfuse")
        return enabled

    def validate_enabled(self) -> None:
        """验证所有显式启用的 Provider 配置完整。"""
        self.langsmith.validate()
        self.langfuse.validate()


# 全局单例 — 使用 double-check locking 懒加载
_tracing_config: TracingConfig | None = None

# 环境变量布尔值的"真"值集合
_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def _env_flag_preferred(*names: str) -> bool:
    """读取第一个存在且非空的环境变量，解析为布尔值。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip().lower() in _TRUTHY_VALUES
    return False


def _first_env_value(*names: str) -> str | None:
    """读取第一个非空的环境变量值。"""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def get_tracing_config() -> TracingConfig:
    """获取当前追踪配置（全局单例，懒加载）。

    使用 double-check locking 确保线程安全且只初始化一次。
    所有配置从环境变量读取。
    """
    global _tracing_config
    if _tracing_config is not None:
        return _tracing_config
    with _config_lock:
        if _tracing_config is not None:
            return _tracing_config
        _tracing_config = TracingConfig(
            langsmith=LangSmithTracingConfig(
                enabled=_env_flag_preferred("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"),
                api_key=_first_env_value("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"),
                project=_first_env_value("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT") or "deer-flow",
                endpoint=_first_env_value("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com",
            ),
            langfuse=LangfuseTracingConfig(
                enabled=_env_flag_preferred("LANGFUSE_TRACING"),
                public_key=_first_env_value("LANGFUSE_PUBLIC_KEY"),
                secret_key=_first_env_value("LANGFUSE_SECRET_KEY"),
                host=_first_env_value("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com",
            ),
        )
        return _tracing_config


def get_enabled_tracing_providers() -> list[str]:
    """返回配置完整且启用的追踪 Provider。"""
    return get_tracing_config().enabled_providers


def get_explicitly_enabled_tracing_providers() -> list[str]:
    """返回显式启用的追踪 Provider（即使配置不完整）。"""
    return get_tracing_config().explicitly_enabled_providers


def validate_enabled_tracing_providers() -> None:
    """验证所有显式启用的 Provider 配置完整。"""
    get_tracing_config().validate_enabled()


def is_tracing_enabled() -> bool:
    """检查是否有任何追踪 Provider 配置完整且启用。"""
    return get_tracing_config().is_configured


def reset_tracing_config() -> None:
    """Discard the cached :class:`TracingConfig` so the next call rebuilds it.

    Public API so that tests do not have to reach into the private
    ``_tracing_config`` module attribute. A future internal rename would
    silently break callers that mutate the attribute directly.
    """
    global _tracing_config
    with _config_lock:
        _tracing_config = None
