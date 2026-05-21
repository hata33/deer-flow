"""
消息处理与信号检测（第 3+4 层辅助模块）

本模块为记忆系统的提取层和中间件层提供消息预处理功能：

1. 消息过滤（filter_messages_for_memory）：
   - 只保留 human 消息和无 tool_calls 的 AI 消息
   - 移除 human 消息中的 <uploaded_files> 块（上传路径是会话级的，不应持久化）
   - 若移除上传标签后消息为空，跳过该条及紧接着的 AI 回复

2. 纠错信号检测（detect_correction）：
   - 在最近 6 条用户消息中匹配中英文纠错关键词
   - 检测到纠错时，LLM 提示词中注入 correction_hint，要求生成高置信度 correction fact

3. 正面反馈信号检测（detect_reinforcement）：
   - 在最近 6 条用户消息中匹配中英文正面反馈关键词
   - 检测到正面反馈时，LLM 提示词中注入 reinforcement_hint

依赖关系：
  - 被 queue.py 调用（中间件层消息过滤）
  - 被 updater.py 调用（提取层信号检测）
  - 被 summarization_hook.py 调用（摘要前刷入记忆）
"""

from __future__ import annotations

import re
from copy import copy
from typing import Any

# ---- 正则模式定义 ----

# 匹配 <uploaded_files>...</uploaded_files> 块
# 用于从 human 消息中移除上传文件路径信息
_UPLOAD_BLOCK_RE = re.compile(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", re.IGNORECASE)

# 纠错信号匹配模式（中英文 11 个模式）
# 检测到后要求 LLM 生成 correction 类别 fact（confidence >= 0.95）
_CORRECTION_PATTERNS = (
    re.compile(r"\bthat(?:'s| is) (?:wrong|incorrect)\b", re.IGNORECASE),  # "that's wrong" / "that is incorrect"
    re.compile(r"\byou misunderstood\b", re.IGNORECASE),  # "you misunderstood"
    re.compile(r"\btry again\b", re.IGNORECASE),  # "try again"
    re.compile(r"\bredo\b", re.IGNORECASE),  # "redo"
    re.compile(r"不对"),  # 中文："不对"
    re.compile(r"你理解错了"),  # 中文："你理解错了"
    re.compile(r"你理解有误"),  # 中文："你理解有误"
    re.compile(r"重试"),  # 中文："重试"
    re.compile(r"重新来"),  # 中文："重新来"
    re.compile(r"换一种"),  # 中文："换一种"
    re.compile(r"改用"),  # 中文："改用"
)

# 正面反馈信号匹配模式（中英文 13 个模式）
# 检测到后要求 LLM 生成 preference/behavior 类别 fact（confidence >= 0.9）
_REINFORCEMENT_PATTERNS = (
    re.compile(r"\byes[,.]?\s+(?:exactly|perfect|that(?:'s| is) (?:right|correct|it))\b", re.IGNORECASE),
    re.compile(r"\bperfect(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bexactly\s+(?:right|correct)\b", re.IGNORECASE),
    re.compile(r"\bthat(?:'s| is)\s+(?:exactly\s+)?(?:right|correct|what i (?:wanted|needed|meant))\b", re.IGNORECASE),
    re.compile(r"\bkeep\s+(?:doing\s+)?that\b", re.IGNORECASE),
    re.compile(r"\bjust\s+(?:like\s+)?(?:that|this)\b", re.IGNORECASE),
    re.compile(r"\bthis is (?:great|helpful)\b(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bthis is what i wanted\b(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"对[，,]?\s*就是这样(?:[。！？!?.]|$)"),  # "对，就是这样"
    re.compile(r"完全正确(?:[。！？!?.]|$)"),  # "完全正确"
    re.compile(r"(?:对[，,]?\s*)?就是这个意思(?:[。！？!?.]|$)"),  # "就是这个意思"
    re.compile(r"正是我想要的(?:[。！？!?.]|$)"),  # "正是我想要的"
    re.compile(r"继续保持(?:[。！？!?.]|$)"),  # "继续保持"
)


# ---- 消息文本提取 ----


def extract_message_text(message: Any) -> str:
    """从消息对象中提取纯文本内容。

    消息的 content 字段可能是：
    - str：直接返回
    - list：多模态内容，提取所有文本部分（str 和 dict["text"]）
    - 其他类型：str() 转换

    用于后续的信号检测（纠错/正面反馈关键词匹配）。
    """
    content = getattr(message, "content", "")
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                text_val = part.get("text")
                if isinstance(text_val, str):
                    text_parts.append(text_val)
        return " ".join(text_parts)
    return str(content)


# ---- 消息过滤 ----


def filter_messages_for_memory(messages: list[Any]) -> list[Any]:
    """过滤对话消息，只保留对记忆更新有用的内容。

    过滤规则：
    1. Human 消息：
       - 移除 <uploaded_files> 块（上传路径是会话级的，不应持久化到记忆）
       - 若移除后消息为空（纯上传消息），跳过该条及紧接着的 AI 回复
    2. AI 消息：
       - 只保留无 tool_calls 的消息（工具调用是执行细节，不含用户信息）
       - 跳过因纯上传消息而标记的 AI 回复

    这种设计确保：
    - 用户上传文件的路径不会写入长期记忆
    - 工具调用的输入输出不占用 LLM 的上下文窗口
    - 只分析用户真正表达的信息和 Agent 的最终回复
    """
    filtered = []
    skip_next_ai = False  # 标记是否跳过下一条 AI 回复（当前 human 消息为纯上传时使用）
    for msg in messages:
        msg_type = getattr(msg, "type", None)

        if msg_type == "human":
            content_str = extract_message_text(msg)
            if "<uploaded_files>" in content_str:
                # 移除上传标签
                stripped = _UPLOAD_BLOCK_RE.sub("", content_str).strip()
                if not stripped:
                    # 纯上传消息，跳过该条及下一条 AI 回复
                    skip_next_ai = True
                    continue
                # 有内容的消息，创建副本并替换 content
                clean_msg = copy(msg)
                clean_msg.content = stripped
                filtered.append(clean_msg)
                skip_next_ai = False
            else:
                filtered.append(msg)
                skip_next_ai = False
        elif msg_type == "ai":
            tool_calls = getattr(msg, "tool_calls", None)
            if not tool_calls:
                # 无工具调用的 AI 回复才保留
                if skip_next_ai:
                    # 上一条 human 是纯上传消息，跳过对应的 AI 回复
                    skip_next_ai = False
                    continue
                filtered.append(msg)

    return filtered


# ---- 信号检测 ----


def detect_correction(messages: list[Any]) -> bool:
    """检测最近的用户消息中是否包含纠错信号。

    在最近 6 条用户消息中搜索 _CORRECTION_PATTERNS 中的关键词。
    若检测到纠错信号，LLM 提示词中会注入 correction_hint，
    要求生成 correction 类别 fact（confidence >= 0.95）。

    纠错信号示例（中英文）：
    "that's wrong", "you misunderstood", "不对", "你理解错了", "重试", "改用"

    Returns:
        True 表示检测到纠错信号
    """
    recent_user_msgs = [msg for msg in messages[-6:] if getattr(msg, "type", None) == "human"]

    for msg in recent_user_msgs:
        content = extract_message_text(msg).strip()
        if content and any(pattern.search(content) for pattern in _CORRECTION_PATTERNS):
            return True

    return False


def detect_reinforcement(messages: list[Any]) -> bool:
    """检测最近的用户消息中是否包含正面反馈信号。

    在最近 6 条用户消息中搜索 _REINFORCEMENT_PATTERNS 中的关键词。
    若检测到正面反馈，LLM 提示词中会注入 reinforcement_hint，
    要求生成 preference/behavior 类别 fact（confidence >= 0.9）。

    注意：在调用方（queue.py / summarization_hook.py）中，
    reinforcement 检测仅在无 correction 信号时执行：
    correction_detected = detect_correction(...)
    reinforcement_detected = not correction_detected and detect_reinforcement(...)
    这意味着纠错优先级高于正面反馈。

    正面反馈示例（中英文）：
    "yes exactly", "perfect", "对，就是这样", "完全正确", "继续保持"

    Returns:
        True 表示检测到正面反馈信号
    """
    recent_user_msgs = [msg for msg in messages[-6:] if getattr(msg, "type", None) == "human"]

    for msg in recent_user_msgs:
        content = extract_message_text(msg).strip()
        if content and any(pattern.search(content) for pattern in _REINFORCEMENT_PATTERNS):
            return True

    return False
