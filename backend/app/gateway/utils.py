"""Gateway 层通用工具函数。

本模块提供 Gateway 各模块共享的辅助工具函数。
所有函数都是纯函数，无副作用，便于测试。

当前函数：
  - sanitize_log_param：剥离控制字符，防止日志注入攻击
"""


def sanitize_log_param(value: str) -> str:
    """剥离控制字符以防止日志注入攻击。

    移除换行符（\\n）、回车符（\\r）和空字符（\\x00），
    防止恶意输入伪造日志行（Log Injection / CRLF Injection）。

    Args:
        value: 待净化的字符串值。

    Returns:
        剥离控制字符后的安全字符串。
    """
    return value.replace("\n", "").replace("\r", "").replace("\x00", "")
