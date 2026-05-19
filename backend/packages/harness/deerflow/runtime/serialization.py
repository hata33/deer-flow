"""
LangChain / LangGraph 对象的规范化序列化模块。

提供将 LangChain 消息对象、Pydantic 模型和 LangGraph 状态字典
转换为纯 JSON 可序列化 Python 结构的单一真实来源。

使用者: ``deerflow.runtime.runs.worker`` (SSE 发布) 和
``app.gateway.routers.threads`` (REST 响应)。
"""

from __future__ import annotations

from typing import Any


def serialize_lc_object(obj: Any) -> Any:
    """递归地将 LangChain 对象序列化为 JSON 可序列化的字典。

    Args:
        obj: 要序列化的对象

    Returns:
        JSON 可序列化的 Python 对象
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: serialize_lc_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_lc_object(item) for item in obj]
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    # Pydantic v1 / 较旧的对象
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    # 最后的手段
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def serialize_channel_values(channel_values: dict[str, Any]) -> dict[str, Any]:
    """序列化通道值，去除内部 LangGraph 键。

    内部键如 ``__pregel_*`` 和 ``__interrupt__`` 被删除，
    以匹配 LangGraph Platform API 返回的内容。

    Args:
        channel_values: 通道值字典

    Returns:
        序列化后的通道值字典
    """
    result: dict[str, Any] = {}
    for key, value in channel_values.items():
        if key.startswith("__pregel_") or key == "__interrupt__":
            continue
        result[key] = serialize_lc_object(value)
    return result


def serialize_messages_tuple(obj: Any) -> Any:
    """序列化 messages-mode 元组 ``(chunk, metadata)``。

    Args:
        obj: 要序列化的对象

    Returns:
        序列化后的列表 [chunk, metadata]
    """
    if isinstance(obj, tuple) and len(obj) == 2:
        chunk, metadata = obj
        return [serialize_lc_object(chunk), metadata if isinstance(metadata, dict) else {}]
    return serialize_lc_object(obj)


def serialize(obj: Any, *, mode: str = "") -> Any:
    """使用特定模式处理序列化 LangChain 对象。

    支持的模式:
    * ``messages`` — obj 是 ``(message_chunk, metadata_dict)``
    * ``values`` — obj 是完整的状态字典；去除 ``__pregel_*`` 键
    * 其他 — 递归 ``model_dump()`` / ``dict()`` 备用方案

    Args:
        obj: 要序列化的对象
        mode: 序列化模式

    Returns:
        序列化后的对象
    """
    if mode == "messages":
        return serialize_messages_tuple(obj)
    if mode == "values":
        return serialize_channel_values(obj) if isinstance(obj, dict) else serialize_lc_object(obj)
    return serialize_lc_object(obj)
