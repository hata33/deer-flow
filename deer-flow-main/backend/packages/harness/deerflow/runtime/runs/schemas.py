"""运行状态和断连模式枚举。"""

from enum import StrEnum


class RunStatus(StrEnum):
    """单次运行的生命周期状态。"""

    pending = "pending"
    running = "running"
    success = "success"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


class DisconnectMode(StrEnum):
    """SSE 消费者断连时的行为策略。"""

    cancel = "cancel"  # 取消运行
    continue_ = "continue"  # 继续运行
