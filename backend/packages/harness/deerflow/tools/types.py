"""工具运行时类型定义（Tool Runtime Types）

本模块定义了 DeerFlow 工具系统使用的核心类型别名。

Runtime 类型：
------------
`Runtime` 是所有 DeerFlow 工具共享的具体运行时类型，定义为：

    Runtime = ToolRuntime[dict[str, Any], ThreadState]

泛型参数说明：
- **ContextT = dict[str, Any]**：上下文参数使用字典而非无界 TypeVar，
  这是为了防止 Pydantic 在 LangChain 调用 `model_dump()` 序列化工具的
  自动生成 `args_schema` 时产生 `PydanticSerializationUnexpectedValue` 警告。
- **StateT = ThreadState**：状态参数绑定到 `ThreadState` 类型，
  提供对线程状态（sandbox、thread_data 等）的类型安全访问。

使用方式：
--------
    from deerflow.tools.types import Runtime

    @tool("my_tool", parse_docstring=True)
    async def my_tool(runtime: Runtime, query: str) -> str:
        thread_id = runtime.context.get("thread_id")
        sandbox_state = runtime.state.get("sandbox")
        ...
"""

from typing import Any

from langchain.tools import ToolRuntime

from deerflow.agents.thread_state import ThreadState

# 所有 DeerFlow 工具使用的具体运行时类型。
# 使用 dict[str, Any] 作为 context 参数类型（而非无界 ContextT TypeVar），
# 防止 Pydantic 在 LangChain 调用 model_dump() 序列化工具的
# 自动生成 args_schema 时产生 PydanticSerializationUnexpectedValue 警告。
Runtime = ToolRuntime[dict[str, Any], ThreadState]
