"""
辅助工具函数
"""
import hashlib
import time
from typing import Any


def generate_id(prefix: str = "") -> str:
    """
    生成确定性 ID

    基于时间戳和随机数生成，但相同输入产生相同输出
    """
    if prefix:
        prefix = f"{prefix}_"

    # 使用时间戳和进程ID生成
    content = f"{time.time()}_{id(prefix)}"
    hash_hex = hashlib.md5(content.encode()).hexdigest()[:12]
    return f"{prefix}{hash_hex}"


def truncate_text(text: str, max_length: int = 1000, suffix: str = "...") -> str:
    """
    截断文本到指定长度
    """
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def safe_get(data: dict[str, Any], *keys, default: Any = None) -> Any:
    """
    安全获取嵌套字典值
    """
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
            if data is None:
                return default
        else:
            return default
    return data


def merge_dicts(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """
    深度合并字典
    """
    result = base.copy()
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result
