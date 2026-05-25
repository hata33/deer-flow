"""LangGraph 状态模式定义。

本模块定义了 Agent 运行时的状态结构 ThreadState，继承自 LangChain AgentState。
状态中的每个字段对应 Agent 交互过程中的一个维度，
LangGraph 通过 Annotated 类型注解实现字段级别的 reducer（合并策略）。

状态字段一览：
  ┌────────────────┬──────────────────────────────────────────────────────┐
  │ 字段           │ 说明                                                │
  ├────────────────┼──────────────────────────────────────────────────────┤
  │ sandbox        │ 沙箱状态（sandbox_id），由 SandboxMiddleware 写入     │
  │ thread_data    │ 线程数据路径（workspace/uploads/outputs）              │
  │ title          │ 自动生成的对话标题                                    │
  │ artifacts      │ 产出物路径列表（带去重 reducer）                       │
  │ todos          │ 任务追踪列表，由 TodoMiddleware 管理                   │
  │ uploaded_files │ 上传文件元信息列表                                    │
  │ viewed_images  │ 已查看图像字典（路径 → {base64, mime_type}），带合并   │
  └────────────────┴──────────────────────────────────────────────────────┘

Reducer 设计：
  - artifacts：merge_artifacts — 合并 + dict.fromkeys 去重 + 保持顺序
  - viewed_images：merge_viewed_images — 合并字典，新值覆盖旧值；
    特殊：空字典 {} 表示清除所有已查看图像（允许中间件处理后清空）

依赖关系：
  - 所有中间件通过 state_schema=ThreadState 与此状态交互
  - LangGraph 的 add_messages reducer 自动处理 messages 字段
"""

from typing import Annotated, NotRequired, TypedDict

from langchain.agents import AgentState


class SandboxState(TypedDict):
    sandbox_id: NotRequired[str | None]


class ThreadDataState(TypedDict):
    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class ViewedImageData(TypedDict):
    base64: str
    mime_type: str


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Reducer for artifacts list - merges and deduplicates artifacts."""
    if existing is None:
        return new or []
    if new is None:
        return existing
    # Use dict.fromkeys to deduplicate while preserving order
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(existing: dict[str, ViewedImageData] | None, new: dict[str, ViewedImageData] | None) -> dict[str, ViewedImageData]:
    """Reducer for viewed_images dict - merges image dictionaries.

    Special case: If new is an empty dict {}, it clears the existing images.
    This allows middlewares to clear the viewed_images state after processing.
    """
    if existing is None:
        return new or {}
    if new is None:
        return existing
    # Special case: empty dict means clear all viewed images
    if len(new) == 0:
        return {}
    # Merge dictionaries, new values override existing ones for same keys
    return {**existing, **new}


class ThreadState(AgentState):
    sandbox: NotRequired[SandboxState | None]
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: NotRequired[list | None]
    uploaded_files: NotRequired[list[dict] | None]
    # image_path -> {base64, mime_type}
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
