"""内置工具集合（Built-in Tools）

本模块导出 DeerFlow 的所有内置工具。这些工具在每次工具装配时都会被包含
（除非被配置文件中的同名工具覆盖）。

内置工具列表：
------------
- **setup_agent**：引导式创建新的自定义代理
- **update_agent**：更新现有自定义代理的 SOUL.md 和 config.yaml
- **present_file_tool**：将输出文件展示给用户查看和下载
- **ask_clarification_tool**：向用户请求澄清（由 ClarificationMiddleware 拦截）
- **view_image_tool**：读取图片文件并转为 base64（仅当模型支持视觉时）
- **task_tool**：将任务委派给专门的子代理执行

加载条件：
--------
- present_file_tool、ask_clarification_tool：始终加载
- view_image_tool：仅当模型的 supports_vision 为 True 时加载
- task_tool：仅当 subagent_enabled=True 时加载
- setup_agent、update_agent：始终加载（在工具装配管线中注册）

注意：
----
tool_search 工具不在此处导出，而是在 tools.py 的 get_available_tools()
中根据 tool_search.enabled 配置动态添加。
invoke_acp_agent 工具也不在此处导出，而是根据 ACP 代理配置动态构建。
"""

from .clarification_tool import ask_clarification_tool
from .present_file_tool import present_file_tool
from .setup_agent_tool import setup_agent
from .task_tool import task_tool
from .update_agent_tool import update_agent
from .view_image_tool import view_image_tool

__all__ = [
    "setup_agent",
    "update_agent",
    "present_file_tool",
    "ask_clarification_tool",
    "view_image_tool",
    "task_tool",
]
