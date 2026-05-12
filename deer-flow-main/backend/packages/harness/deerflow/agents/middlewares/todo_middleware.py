"""扩展 TodoListMiddleware 的中间件，增加了上下文丢失检测。

当消息历史被截断（例如由 SummarizationMiddleware）时，原始的 write_todos 工具调用
及其 ToolMessage 可能已滚出活跃的上下文窗口。此中间件检测该情况并注入提醒消息，
使模型仍然知道未完成的待办列表。
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import PlanningState, Todo
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime


def _todos_in_messages(messages: list[Any]) -> bool:
    """如果 *messages* 中的任何 AIMessage 包含 write_todos 工具调用，则返回 True。"""
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "write_todos":
                    return True
    return False


def _reminder_in_messages(messages: list[Any]) -> bool:
    """如果 *messages* 中已存在 todo_reminder HumanMessage，则返回 True。"""
    for msg in messages:
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) == "todo_reminder":
            return True
    return False


def _format_todos(todos: list[Todo]) -> str:
    """将 Todo 项列表格式化为可读字符串。"""
    lines: list[str] = []
    for todo in todos:
        status = todo.get("status", "pending")
        content = todo.get("content", "")
        lines.append(f"- [{status}] {content}")
    return "\n".join(lines)


class TodoMiddleware(TodoListMiddleware):
    """扩展 TodoListMiddleware，增加了 `write_todos` 上下文丢失检测。

    当原始的 `write_todos` 工具调用已从消息历史中被截断（例如摘要化后），
    模型会失去对当前待办列表的感知。此中间件在 `before_model` / `abefore_model`
    中检测该缺口并注入提醒消息，使模型可以继续跟踪进度。
    """

    @override
    def before_model(
        self,
        state: PlanningState,
        runtime: Runtime,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """当 write_todos 已离开上下文窗口时注入待办列表提醒。"""
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]
        if not todos:
            return None

        messages = state.get("messages") or []
        if _todos_in_messages(messages):
            # write_todos 在上下文中仍然可见——无需处理。
            return None

        if _reminder_in_messages(messages):
            # 提醒已注入且尚未被截断。
            return None

        # 待办列表存在于状态中，但原始的 write_todos 调用已消失。
        # 注入提醒作为 HumanMessage，使模型保持感知。
        formatted = _format_todos(todos)
        reminder = HumanMessage(
            name="todo_reminder",
            content=(
                "<system_reminder>\n"
                "Your todo list from earlier is no longer visible in the current context window, "
                "but it is still active. Here is the current state:\n\n"
                f"{formatted}\n\n"
                "Continue tracking and updating this todo list as you work. "
                "Call `write_todos` whenever the status of any item changes.\n"
                "</system_reminder>"
            ),
        )
        return {"messages": [reminder]}

    @override
    async def abefore_model(
        self,
        state: PlanningState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """before_model 的异步版本。"""
        return self.before_model(state, runtime)
