"""LangSmith 追踪配置。

本模块定义了 DeerFlow 的 LangSmith 追踪系统配置。
LangSmith 是 LangChain 的可观测性平台，用于追踪和调试 LLM 应用的运行过程。

配置来源：
    完全通过环境变量配置（不从 config.yaml 读取）。
    支持新的 LANGSMITH_* 和旧的 LANGCHAIN_* 环境变量，前者优先。

环境变量优先级：
    - enabled:  LANGSMITH_TRACING > LANGCHAIN_TRACING_V2 > LANGCHAIN_TRACING
    - api_key:  LANGSMITH_API_KEY > LANGCHAIN_API_KEY
    - project:  LANGSMITH_PROJECT > LANGCHAIN_PROJECT（默认 "deer-flow"）
    - endpoint: LANGSMITH_ENDPOINT > LANGCHAIN_ENDPOINT（默认 https://api.smith.langchain.com）

布尔值解析：
    环境变量中以下值被视为 True（不区分大小写）：1、true、yes、on。
    其他非空值被视为 False。未设置的环境变量不参与判断。

线程安全：
    使用双重检查锁定（double-checked locking）模式确保线程安全的延迟初始化。
    _config_lock 保护 _tracing_config 的首次初始化。

配置示例（环境变量）：
    ```bash
    export LANGSMITH_TRACING=true
    export LANGSMITH_API_KEY=lsv2_sk_...
    export LANGSMITH_PROJECT=deer-flow
    ```
"""
import logging
import os
import threading

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 线程安全锁，保护 _tracing_config 的首次初始化
_config_lock = threading.Lock()


class TracingConfig(BaseModel):
    """LangSmith 追踪配置。

    Attributes:
        enabled: 是否启用追踪。
        api_key: LangSmith API 密钥。
        project: 追踪项目名称。
        endpoint: LangSmith API 端点 URL。
    """

    enabled: bool = Field(...)
    api_key: str | None = Field(...)
    project: str = Field(...)
    endpoint: str = Field(...)

    @property
    def is_configured(self) -> bool:
        """检查追踪是否已完全配置（已启用且有 API Key）。"""
        return self.enabled and bool(self.api_key)


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_tracing_config: TracingConfig | None = None

# 布尔值真值集合（不区分大小写）
_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def _env_flag_preferred(*names: str) -> bool:
    """返回第一个存在且非空的环境变量的布尔值。

    接受的真值（不区分大小写）：1、true、yes、on。
    其他非空值被视为 False。所有命名的变量都未设置时返回 False。

    Args:
        *names: 按优先级排列的环境变量名列表。

    Returns:
        第一个找到的环境变量的布尔解析值。
    """
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip().lower() in _TRUTHY_VALUES
    return False


def _first_env_value(*names: str) -> str | None:
    """返回第一个非空的环境变量值。

    Args:
        *names: 按优先级排列的环境变量名列表。

    Returns:
        第一个找到的非空环境变量值，全部为空时返回 None。
    """
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def get_tracing_config() -> TracingConfig:
    """获取当前追踪配置。

    从环境变量中读取配置。LANGSMITH_* 变量优先于旧的 LANGCHAIN_* 变量。

    对于布尔标志（enabled），优先级列表中第一个存在且非空的变量是唯一权威来源——
    其值被解析后直接返回，不再查询其余候选变量。

    优先级：
        enabled:  LANGSMITH_TRACING > LANGCHAIN_TRACING_V2 > LANGCHAIN_TRACING
        api_key:  LANGSMITH_API_KEY > LANGCHAIN_API_KEY
        project:  LANGSMITH_PROJECT > LANGCHAIN_PROJECT（默认 "deer-flow"）
        endpoint: LANGSMITH_ENDPOINT > LANGCHAIN_ENDPOINT（默认 https://api.smith.langchain.com）

    使用双重检查锁定确保线程安全的延迟初始化。

    Returns:
        当前追踪配置。
    """
    global _tracing_config
    # 第一次检查（无锁，快速路径）
    if _tracing_config is not None:
        return _tracing_config
    with _config_lock:
        # 第二次检查（有锁，防止竞态条件）
        if _tracing_config is not None:
            return _tracing_config
        _tracing_config = TracingConfig(
            # 兼容旧的 LANGCHAIN_* 和新的 LANGSMITH_* 环境变量
            enabled=_env_flag_preferred("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"),
            api_key=_first_env_value("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"),
            project=_first_env_value("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT") or "deer-flow",
            endpoint=_first_env_value("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com",
        )
        return _tracing_config


def is_tracing_enabled() -> bool:
    """检查 LangSmith 追踪是否已启用并完成配置。

    Returns:
        True 如果追踪已启用且有 API Key。
    """
    return get_tracing_config().is_configured
