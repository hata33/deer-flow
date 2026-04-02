"""
工具注册表

管理工具的注册、查找和调用
"""
import logging
import inspect
from typing import Any, Callable

from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    工具注册表

    管理所有可用工具
    """

    _instance: 'ToolRegistry' | None = None

    def __init__(self):
        self._tools: dict[str, StructuredTool] = {}

    @classmethod
    def get_instance(cls) -> 'ToolRegistry':
        """获取单例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls

    def register(self, tool: StructuredTool) -> None:
        """注册工具"""
        self._tools[tool.name] = tool
        logger.debug(f"注册工具: {tool.name}")

    def unregister(self, name: str) -> None:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            logger.debug(f"注销工具: {name}")

    def get(self, name: str) -> StructuredTool | None:
        """获取工具"""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """列出所有工具名称"""
        return list(self._tools.keys())

    def get_all(self) -> list[StructuredTool]:
        """获取所有工具"""
        return list(self._tools.values())


def tool(
    name: str | None = None,
    description: str | None = None,
) -> Callable:
    """
    工具装饰器

    用于将函数注册为工具

    用法:
    ```python
    @tool(name="my_tool", description="My custom tool")
    def my_function(arg1: str, arg2: int) -> str:
        return f"结果: {arg1} - {arg2}"
    ```
    """
    def decorator(func: Callable) -> Callable:
        # 使用 LangChain 的 from_function 自动处理 schema
        tool_name = name or func.__name__
        tool_desc = description or func.__doc__ or f"工具: {tool_name}"

        try:
            # 让 LangChain 自动从函数签名生成 schema
            structured_tool = StructuredTool.from_function(
                func,
                name=tool_name,
                description=tool_desc,
            )

            # 注册工具
            registry = ToolRegistry.get_instance()
            registry.register(structured_tool)

        except Exception as e:
            logger.error(f"注册工具 {tool_name} 失败: {e}")

        return func

    return decorator


def get_tool_registry() -> ToolRegistry:
    """获取工具注册表单例"""
    return ToolRegistry.get_instance()


def get_registered_tools() -> list[StructuredTool]:
    """获取所有已注册的工具"""
    return get_tool_registry().get_all()


def register_tool(
    func: Callable,
    name: str | None = None,
    description: str | None = None,
) -> None:
    """
    手动注册工具函数

    Args:
        func: 工具函数
        name: 工具名称
        description: 工具描述
    """
    tool_name = name or func.__name__
    tool_desc = description or func.__doc__ or f"工具: {tool_name}"

    try:
        structured_tool = StructuredTool.from_function(
            func,
            name=tool_name,
            description=tool_desc,
        )

        registry = ToolRegistry.get_instance()
        registry.register(structured_tool)

    except Exception as e:
        logger.error(f"注册工具 {tool_name} 失败: {e}")
