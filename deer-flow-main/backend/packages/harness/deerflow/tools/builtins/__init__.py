"""内置工具包。

聚合所有 DeerFlow 内置工具，供 ``get_available_tools()`` 按需组装。
工具列表：
- ask_clarification_tool — 请求用户澄清（被 ClarificationMiddleware 拦截）
- present_file_tool      — 向用户展示输出文件
- setup_agent            — 动态创建自定义 agent
- task_tool              — 将任务委派给子代理执行
- view_image_tool        — 读取图片文件（仅视觉模型可用）
"""

from .clarification_tool import ask_clarification_tool
from .present_file_tool import present_file_tool
from .setup_agent_tool import setup_agent
from .task_tool import task_tool
from .view_image_tool import view_image_tool

__all__ = [
    "setup_agent",
    "present_file_tool",
    "ask_clarification_tool",
    "view_image_tool",
    "task_tool",
]
