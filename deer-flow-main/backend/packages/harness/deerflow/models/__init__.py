"""模型工厂模块。

提供 create_chat_model 工厂函数，根据 config.yaml 中的模型配置
通过反射机制动态创建 LangChain BaseChatModel 实例。
"""

from .factory import create_chat_model

__all__ = ["create_chat_model"]
