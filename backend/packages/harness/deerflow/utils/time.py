"""ISO 8601 时间戳工具模块 —— Gateway 和嵌入式运行时的统一时间格式。

本模块为 DeerFlow 系统提供统一的时间戳生成和格式转换工具，确保所有
组件使用一致的 ISO 8601 UTC 格式进行时间序列化。

核心设计动机：
    DeerFlow 的 ``Thread`` 和 ``Run`` 等核心数据模型使用 ISO 8601 字符串
    作为时间戳格式，以匹配 LangGraph Platform 的 schema 约定（参见
    ``langgraph_sdk.schema.Thread``，其中 ``created_at`` / ``updated_at``
    字段为 ``datetime`` 类型，JSON 序列化为 ISO 8601）。

    所有时间戳生成应统一通过 :func:`now_iso` 进行，以确保线上格式在
    API 端点、嵌入式 ``RunManager`` 和 Gateway 写入的检查点元数据之间
    保持一致。

向后兼容：
    :func:`coerce_iso` 为旧版记录提供前向兼容的读取路径。历史版本中
    部分时间戳以 ``str(time.time())`` 格式存储（Unix 浮点数字符串），
    此函数可自动识别并转换这些旧格式，无需一次性数据迁移。

模块导出：
    - :func:`now_iso` —— 获取当前 UTC 时间的 ISO 8601 字符串
    - :func:`coerce_iso` —— 将任意格式的时间戳转换为 ISO 8601

典型用法::

    from deerflow.utils.time import now_iso, coerce_iso

    # 写入新记录时
    thread.created_at = now_iso()  # "2026-04-27T03:19:46.511479+00:00"

    # 读取旧记录时（自动兼容旧格式）
    updated = coerce_iso(record.get("updated_at"))
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

__all__ = ["coerce_iso", "now_iso"]

# 匹配 Unix 时间戳字符串的正则表达式。
# 历史版本使用 ``str(time.time())`` 存储时间戳，格式为 10 位秒数 + 可选小数部分。
# 使用 10 位数字锚定，避免意外将 ISO 年份（如 ``"2026"``）误判为时间戳。
# 此模式在 2286 年之前有效（10 位 Unix 时间戳的有效范围）。
_UNIX_TIMESTAMP_PATTERN = re.compile(r"^\d{10}(?:\.\d+)?$")


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    使用 ``datetime.now(UTC)`` 获取带时区信息的当前时间，
    然后通过 ``isoformat()`` 序列化为 ISO 8601 格式。
    输出包含完整的微秒精度和 UTC 时区偏移量。

    Returns:
        ISO 8601 格式的 UTC 时间字符串。
        示例：``"2026-04-27T03:19:46.511479+00:00"``

    Note:
        所有 DeerFlow 组件的时间戳生成都应通过此函数进行，
        确保线上格式的一致性。
    """
    return datetime.now(UTC).isoformat()


def coerce_iso(value: object) -> str:
    """尽力将存储的时间戳转换为 ISO 8601 字符串。

    此函数为多格式时间戳提供统一的转换入口，支持以下输入类型：
    1. **None / 空字符串** → 返回空字符串 ``""``
    2. **bool** → 转为字符串（``bool`` 是 ``int`` 的子类，
       必须在 ``int`` 检查之前处理，否则 ``True`` 会变成 ``"1970-01-01..."``）
    3. **datetime** → 标准化为 UTC 后转为 ISO 8601
       （无时区信息的 datetime 假定为 UTC）
    4. **int / float** → 视为 Unix 时间戳，转换为 ISO 8601
    5. **str** → 如果匹配 Unix 时间戳模式则转换，否则原样返回
    6. **其他类型** → 兜底 ``str()`` 转换

    设计意图是避免一次性数据迁移：旧记录中的 ``str(time.time())`` 格式
    在读取时自动转换为新格式，而新记录始终通过 ``now_iso()`` 生成 ISO 格式。

    Args:
        value: 待转换的时间戳值，可以是多种类型。

    Returns:
        ISO 8601 格式字符串，或空字符串，或原始值的字符串表示。

    Note:
        - ``datetime`` 必须在 ``int``/``float`` 之前检查，因为 ``str(datetime)``
          使用空格分隔符（``"YYYY-MM-DD HH:MM:SS"``），不符合严格的 ISO 8601。
        - ``bool`` 必须在 ``int`` 之前检查，因为 ``bool`` 是 ``int`` 的子类。
        - Unix 时间戳转换失败时（溢出等），返回原始值的字符串形式而非抛出异常。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        # ``bool`` 是 ``int`` 的子类 —— 如果不在此处拦截，
        # True 会被当作 Unix 时间戳 1（1970-01-01）处理
        return str(value)
    if isinstance(value, datetime):
        # ``datetime`` 必须在 ``int``/``float`` 检查之前处理；
        # ``str(datetime)`` 会产生空格分隔的格式（"YYYY-MM-DD HH:MM:SS+00:00"），
        # 不符合严格的 ISO 8601 规范（要求 T 分隔符）
        if value.tzinfo is None:
            # 无时区信息的 datetime 假定为 UTC，添加时区标记
            value = value.replace(tzinfo=UTC)
        else:
            # 有时区信息的 datetime 转换为 UTC
            value = value.astimezone(UTC)
        return value.isoformat()
    if isinstance(value, (int, float)):
        # 整数或浮点数视为 Unix 时间戳，转换为 UTC datetime 后格式化
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            # ValueError: 无效的时间戳值
            # OverflowError: 超出 datetime 范围
            # OSError: 平台限制（某些系统不支持负时间戳）
            return str(value)
    if isinstance(value, str):
        # 字符串类型：先检查是否为 Unix 时间戳格式（历史兼容）
        if _UNIX_TIMESTAMP_PATTERN.match(value):
            try:
                return datetime.fromtimestamp(float(value), UTC).isoformat()
            except (ValueError, OverflowError, OSError):
                return value
        # 已经是 ISO 格式或其他格式的字符串，原样返回
        return value
    # 兜底：未知类型直接转字符串
    return str(value)
