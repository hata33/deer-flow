"""LangChain / LangGraph 对象的规范化序列化。

将 LangChain 消息对象、Pydantic 模型和 LangGraph 状态字典转为纯 JSON 可序列化结构。
消费者：deerflow.runtime.runs.worker（SSE 发布）和 app.gateway.routers.threads（REST 响应）。
"""

from __future__ import annotations

from typing import Any


def serialize_lc_object(obj: Any) -> Any:
    """递归序列化 LangChain 对象为 JSON 可序列化字典。

    处理优先级：基础类型 → 字典/列表 → Pydantic v2 model_dump → Pydantic v1 dict → str。
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
    # Pydantic v1 / 旧对象
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    # 兜底：转为字符串
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def serialize_channel_values(channel_values: dict[str, Any]) -> dict[str, Any]:
    """序列化 channel values，移除 LangGraph 内部键（__pregel_*、__interrupt__）。

    与 LangGraph Platform API 返回格式对齐。
    """
    result: dict[str, Any] = {}
    for key, value in channel_values.items():
        if key.startswith("__pregel_") or key == "__interrupt__":
            continue
        result[key] = serialize_lc_object(value)
    return result


def serialize_messages_tuple(obj: Any) -> Any:
    """序列化 messages 模式的元组 (chunk, metadata)。"""
    if isinstance(obj, tuple) and len(obj) == 2:
        chunk, metadata = obj
        return [serialize_lc_object(chunk), metadata if isinstance(metadata, dict) else {}]
    return serialize_lc_object(obj)


def serialize(obj: Any, *, mode: str = "") -> Any:
    """按模式序列化 LangChain 对象。

    - ``messages`` — obj 是 (message_chunk, metadata_dict)
    - ``values`` — obj 是完整状态字典，移除 __pregel_* 键
    - 其他 — 递归 model_dump() / dict() 兜底
    """
    if mode == "messages":
        return serialize_messages_tuple(obj)
    if mode == "values":
        return serialize_channel_values(obj) if isinstance(obj, dict) else serialize_lc_object(obj)
    return serialize_lc_object(obj)
