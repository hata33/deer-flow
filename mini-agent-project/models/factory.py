"""
模型工厂

根据配置创建 LangChain ChatModel 实例
"""
import enum
import logging

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

from config import get_app_config
from config.model_config import ModelConfig

logger = logging.getLogger(__name__)


class ModelProvider(str, enum.Enum):
    """支持的模型提供商"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    # 可扩展更多提供商


def create_chat_model(
    name: str | None = None,
    temperature: float | None = None,
) -> ChatOpenAI | ChatAnthropic:
    """
    创建聊天模型实例

    Args:
        name: 模型名称，如果为 None 则使用默认模型
        temperature: 温度参数，覆盖配置中的值

    Returns:
        LangChain ChatModel 实例

    Raises:
        ValueError: 如果模型配置不存在或提供商不支持
    """
    config = get_app_config()
    model_config = config.get_model_config(name)

    logger.info(f"创建模型: {model_config.name} ({model_config.provider})")

    # 覆盖温度参数
    if temperature is not None:
        model_config.temperature = temperature

    provider = ModelProvider(model_config.provider)

    # 根据提供商创建模型
    if provider == ModelProvider.OPENAI:
        return ChatOpenAI(
            **model_config.get_init_kwargs()
        )
    elif provider == ModelProvider.ANTHROPIC:
        return ChatAnthropic(
            **model_config.get_init_kwargs()
        )
    else:
        raise ValueError(f"不支持的模型提供商: {provider}")


def list_available_models() -> list[str]:
    """列出所有可用模型名称"""
    config = get_app_config()
    return [m.name for m in config.models]
