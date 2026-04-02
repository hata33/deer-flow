"""
代理状态定义

定义代理运行时的状态结构
"""
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage


@dataclass
class StateSnapshot:
    """
    状态快照

    用于保存和恢复代理状态
    """
    timestamp: datetime = field(default_factory=datetime.now)
    messages: list[BaseMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "messages": [self._message_to_dict(m) for m in self.messages],
            "metadata": self.metadata,
        }

    def _message_to_dict(self, message: BaseMessage) -> dict[str, Any]:
        """转换消息为字典"""
        return {
            "type": message.type,
            "content": message.content,
        }


@dataclass
class AgentState:
    """
    代理状态

    包含消息历史、工具调用、元数据等
    """
    messages: list[BaseMessage] = field(default_factory=list)
    input: str = ""
    output: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # 扩展字段
    artifacts: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def add_message(self, message: BaseMessage) -> None:
        """添加消息"""
        self.messages.append(message)

    def get_last_message(self) -> BaseMessage | None:
        """获取最后一条消息"""
        return self.messages[-1] if self.messages else None

    def create_snapshot(self) -> StateSnapshot:
        """创建状态快照"""
        return StateSnapshot(
            messages=self.messages.copy(),
            metadata=self.metadata.copy(),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "messages": [m.content for m in self.messages],
            "input": self.input,
            "output": self.output,
            "tool_calls": self.tool_calls,
            "metadata": self.metadata,
            "artifacts": self.artifacts,
        }
