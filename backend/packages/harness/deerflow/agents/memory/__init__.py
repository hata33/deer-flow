"""
DeerFlow 记忆系统（agents/memory 包入口）

本包实现 DeerFlow 的核心差异化功能——跨会话记忆系统。

四层架构：
  第 1 层（注入）：prompt.py → format_memory_for_injection()
    在 Agent 构建时将记忆格式化后注入 system prompt
  第 2 层（存储）：storage.py → MemoryStorage / FileMemoryStorage
    负责记忆数据的持久化读写（JSON 文件 + mtime 缓存 + 原子写入）
  第 3 层（提取）：updater.py → MemoryUpdater
    LLM 分析对话 → 结构化 JSON 更新指令 → 合并去重 → 持久化
  第 4 层（中间件）：queue.py → MemoryUpdateQueue
    防抖队列 + threading.Timer，Agent 对话完成后自动触发记忆更新

完整生命周期：
  第 N 轮对话:
    make_lead_agent() → _get_memory_context() → 读取 memory.json → 注入 <memory>
    agent.astream() → 用户与 Agent 交互
    MemoryMiddleware.after_agent() → queue.add() → 排队，等 30s

  30s 无新消息后:
    _process_queue() → MemoryUpdater.update_memory()
    → LLM 分析对话 → JSON 更新指令 → storage.save()

  第 N+1 轮对话:
    make_lead_agent() → 读到上一轮更新的记忆 ✓

模块导出：
  - 提示词工具：MEMORY_UPDATE_PROMPT, FACT_EXTRACTION_PROMPT,
    format_memory_for_injection, format_conversation_for_update
  - 队列：ConversationContext, MemoryUpdateQueue,
    get_memory_queue, reset_memory_queue
  - 存储：MemoryStorage, FileMemoryStorage, get_memory_storage
  - 更新器：MemoryUpdater, clear_memory_data, delete_memory_fact,
    get_memory_data, reload_memory_data, update_memory_from_conversation
"""

from deerflow.agents.memory.prompt import (
    FACT_EXTRACTION_PROMPT,
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
    format_memory_for_injection,
)
from deerflow.agents.memory.queue import (
    ConversationContext,
    MemoryUpdateQueue,
    get_memory_queue,
    reset_memory_queue,
)
from deerflow.agents.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    get_memory_storage,
)
from deerflow.agents.memory.updater import (
    MemoryUpdater,
    clear_memory_data,
    delete_memory_fact,
    get_memory_data,
    reload_memory_data,
    update_memory_from_conversation,
)

__all__ = [
    # 提示词工具（第 1+3 层）
    "MEMORY_UPDATE_PROMPT",
    "FACT_EXTRACTION_PROMPT",
    "format_memory_for_injection",
    "format_conversation_for_update",
    # 队列（第 4 层）
    "ConversationContext",
    "MemoryUpdateQueue",
    "get_memory_queue",
    "reset_memory_queue",
    # 存储（第 2 层）
    "MemoryStorage",
    "FileMemoryStorage",
    "get_memory_storage",
    # 更新器（第 3 层）
    "MemoryUpdater",
    "clear_memory_data",
    "delete_memory_fact",
    "get_memory_data",
    "reload_memory_data",
    "update_memory_from_conversation",
]
