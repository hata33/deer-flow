"""线程状态定义模块。

定义了 DeerFlow 智能体使用的线程级状态模式（ThreadState），
包括沙箱状态、线程数据、标题、产物、待办列表、上传文件和已查看图片等。
"""

from typing import Annotated, NotRequired, TypedDict

from langchain.agents import AgentState


class SandboxState(TypedDict):
    """沙箱状态，存储沙箱标识符。"""
    sandbox_id: NotRequired[str | None]


class ThreadDataState(TypedDict):
    """线程数据目录状态，存储工作区、上传目录和输出目录的路径。"""
    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class ViewedImageData(TypedDict):
    """已查看图片数据，包含 base64 编码和 MIME 类型。"""
    base64: str
    mime_type: str


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """产物列表的归并函数 - 合并并去重产物列表，保持顺序。"""
    if existing is None:
        return new or []
    if new is None:
        return existing
    # 使用 dict.fromkeys 去重，同时保持顺序
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(existing: dict[str, ViewedImageData] | None, new: dict[str, ViewedImageData] | None) -> dict[str, ViewedImageData]:
    """已查看图片字典的归并函数 - 合并图片字典。

    特殊情况：如果 new 是空字典 {}，则清空所有已查看图片。
    这允许中间件在处理完成后清空 viewed_images 状态。
    """
    if existing is None:
        return new or {}
    if new is None:
        return existing
    # 空字典表示清空所有已查看图片
    if len(new) == 0:
        return {}
    # 合并字典，新值覆盖同名旧值
    return {**existing, **new}


class ThreadState(AgentState):
    """DeerFlow 线程状态，扩展自 AgentState。

    字段说明：
    - sandbox: 沙箱状态（sandbox_id）
    - thread_data: 线程数据目录路径（workspace/uploads/outputs）
    - title: 自动生成的线程标题
    - artifacts: 产物路径列表（自动去重归并）
    - todos: 待办事项列表
    - uploaded_files: 上传文件元数据列表
    - viewed_images: 已查看图片字典（image_path -> {base64, mime_type}）
    """
    sandbox: NotRequired[SandboxState | None]
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: NotRequired[list | None]
    uploaded_files: NotRequired[list[dict] | None]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]  # image_path -> {base64, mime_type}
