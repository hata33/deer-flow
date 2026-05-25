"""文件展示工具（Present File Tool）

本模块实现了 `present_files` 工具，用于将输出文件展示给用户查看和下载。

功能说明：
--------
- 将代理创建的文件标记为"已展示"，使其在客户端界面中可见
- 支持同时展示多个文件
- 只能展示 `/mnt/user-data/outputs/` 目录下的文件（安全限制）

路径规范化：
----------
工具接受两种路径格式：
1. 虚拟沙箱路径：如 `/mnt/user-data/outputs/report.md`
2. 宿主机端线程输出路径：如 `/app/backend/.deer-flow/threads/<thread>/user-data/outputs/report.md`

两种格式都会被规范化为统一的虚拟路径：`/mnt/user-data/outputs/<relative_path>`

安全限制：
--------
- 只能展示当前线程 outputs 目录下的文件
- 不允许展示 outputs 目录以外的文件（防止路径遍历攻击）
- 需要有效的 thread_id 和 outputs_path

状态更新：
--------
工具返回一个 `Command` 对象，包含：
- `artifacts`：规范化后的文件路径列表（由 merge_artifacts reducer 处理去重和合并）
- `messages`：成功消息（ToolMessage）

使用流程：
--------
1. 代理创建文件并移动到 `/mnt/user-data/outputs/` 目录
2. 代理调用 present_files 工具展示文件
3. 文件路径被添加到会话状态的 artifacts 列表中
4. 客户端界面从 artifacts 列表中渲染文件
"""

from pathlib import Path
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.types import Command

from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tools.types import Runtime

# 输出文件的虚拟路径前缀常量
OUTPUTS_VIRTUAL_PREFIX = f"{VIRTUAL_PATH_PREFIX}/outputs"


def _get_thread_id(runtime: Runtime) -> str | None:
    """从运行时上下文或 RunnableConfig 中解析当前线程 ID。

    查找优先级：
    1. runtime.context["thread_id"]
    2. runtime.config["configurable"]["thread_id"]
    3. LangGraph get_config()["configurable"]["thread_id"]
    """
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return thread_id

    runtime_config = getattr(runtime, "config", None) or {}
    thread_id = runtime_config.get("configurable", {}).get("thread_id")
    if thread_id:
        return thread_id

    try:
        return get_config().get("configurable", {}).get("thread_id")
    except RuntimeError:
        return None


def _normalize_presented_filepath(
    runtime: Runtime,
    filepath: str,
) -> str:
    """将展示文件路径规范化为 `/mnt/user-data/outputs/*` 格式。

    接受两种输入格式：
    - 虚拟沙箱路径：如 `/mnt/user-data/outputs/report.md`
    - 宿主机端线程输出路径：如 `/app/backend/.deer-flow/threads/<thread>/user-data/outputs/report.md`

    规范化过程：
    1. 验证 runtime.state 存在
    2. 解析 thread_id 和 outputs_path
    3. 将虚拟路径解析为实际文件系统路径
    4. 验证文件路径在 outputs 目录内
    5. 返回规范化后的虚拟路径

    Args:
        runtime: 工具运行时（包含线程状态）
        filepath: 待规范化的文件路径

    Returns:
        规范化后的虚拟路径字符串

    Raises:
        ValueError: 如果运行时元数据缺失或路径在 outputs 目录之外
    """
    if runtime.state is None:
        raise ValueError("Thread runtime state is not available")

    thread_id = _get_thread_id(runtime)
    if not thread_id:
        raise ValueError("Thread ID is not available in runtime context or runtime config")

    thread_data = runtime.state.get("thread_data") or {}
    outputs_path = thread_data.get("outputs_path")
    if not outputs_path:
        raise ValueError("Thread outputs path is not available in runtime state")

    outputs_dir = Path(outputs_path).resolve()

    # 处理虚拟路径格式
    stripped = filepath.lstrip("/")
    virtual_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
    if stripped == virtual_prefix or stripped.startswith(virtual_prefix + "/"):
        try:
            actual_path = get_paths().resolve_virtual_path(thread_id, filepath, user_id=get_effective_user_id())
        except TypeError:
            # 兼容不支持 user_id 参数的旧版 resolve_virtual_path
            actual_path = get_paths().resolve_virtual_path(thread_id, filepath)
    else:
        # 宿主机端路径：直接解析
        actual_path = Path(filepath).expanduser().resolve()

    # 安全检查：确保文件在 outputs 目录内
    try:
        relative_path = actual_path.relative_to(outputs_dir)
    except ValueError as exc:
        raise ValueError(f"Only files in {OUTPUTS_VIRTUAL_PREFIX} can be presented: {filepath}") from exc

    return f"{OUTPUTS_VIRTUAL_PREFIX}/{relative_path.as_posix()}"


@tool("present_files", parse_docstring=True)
def present_file_tool(
    runtime: Runtime,
    filepaths: list[str],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Make files visible to the user for viewing and rendering in the client interface.

    使文件在客户端界面中可见，供用户查看和渲染。

    何时使用 present_files 工具：
    - 使任何文件可供用户查看、下载或交互
    - 同时展示多个相关文件
    - 创建了应展示给用户的文件后

    何时不应使用 present_files 工具：
    - 仅需要读取文件内容进行内部处理时
    - 用于不需要用户查看的临时或中间文件

    注意事项：
    - 在创建文件并移动到 `/mnt/user-data/outputs` 目录后，应调用此工具。
    - 此工具可以安全地与其他工具并行调用。状态更新由 reducer 处理以防止冲突。

    Args:
        filepaths: 要展示给用户的绝对文件路径列表。**仅支持** `/mnt/user-data/outputs` 目录下的文件。
    """
    try:
        normalized_paths = [_normalize_presented_filepath(runtime, filepath) for filepath in filepaths]
    except ValueError as exc:
        return Command(
            update={"messages": [ToolMessage(f"Error: {exc}", tool_call_id=tool_call_id)]},
        )

    # merge_artifacts reducer 会处理合并和去重
    return Command(
        update={
            "artifacts": normalized_paths,
            "messages": [ToolMessage("Successfully presented files", tool_call_id=tool_call_id)],
        },
    )
