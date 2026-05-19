"""
运行状态和断开连接模式枚举。
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """单个运行的生命周期状态。

    状态值:
    - pending: 待处理
    - running: 正在运行
    - success: 成功完成
    - error: 发生错误
    - timeout: 超时
    - interrupted: 被中断
    """

    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


class DisconnectMode(StrEnum):
    """SSE 消费者断开连接时的行为。

    模式值:
    - cancel: 取消运行
    - continue_: 继续运行
    """

    cancel = "cancel"
    continue_ = "continue"
