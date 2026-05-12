"""记忆更新器，负责读取、写入和更新记忆数据。

通过 LLM 分析对话内容，提取用户上下文、偏好和事实，
并以原子方式持久化到存储中。
"""

import json
import logging
import math
import re
import uuid
from datetime import datetime
from typing import Any

from deerflow.agents.memory.prompt import (
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
)
from deerflow.agents.memory.storage import create_empty_memory, get_memory_storage
from deerflow.config.memory_config import get_memory_config
from deerflow.models import create_chat_model

logger = logging.getLogger(__name__)


def _create_empty_memory() -> dict[str, Any]:
    """向后兼容的空记忆创建包装器。"""
    return create_empty_memory()


def _save_memory_to_file(memory_data: dict[str, Any], agent_name: str | None = None) -> bool:
    """向后兼容的记忆保存包装器。"""
    return get_memory_storage().save(memory_data, agent_name)


def get_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """通过存储提供者获取当前记忆数据。"""
    return get_memory_storage().load(agent_name)


def reload_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """通过存储提供者重新加载记忆数据。"""
    return get_memory_storage().reload(agent_name)


def import_memory_data(memory_data: dict[str, Any], agent_name: str | None = None) -> dict[str, Any]:
    """通过存储提供者持久化导入的记忆数据。

    Args:
        memory_data: 要持久化的完整记忆数据。
        agent_name: 如果提供，导入到按智能体的记忆中。

    Returns:
        存储标准化后的已保存记忆数据。

    Raises:
        OSError: 如果持久化导入的记忆失败。
    """
    storage = get_memory_storage()
    if not storage.save(memory_data, agent_name):
        raise OSError("Failed to save imported memory data")
    return storage.load(agent_name)


def clear_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    """清除所有已存储的记忆数据并持久化空结构。"""
    cleared_memory = create_empty_memory()
    if not _save_memory_to_file(cleared_memory, agent_name):
        raise OSError("Failed to save cleared memory data")
    return cleared_memory


def _validate_confidence(confidence: float) -> float:
    """验证持久化的事实置信度，确保存储的 JSON 符合标准。"""
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise ValueError("confidence")
    return confidence


def create_memory_fact(
    content: str,
    category: str = "context",
    confidence: float = 0.5,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """创建新的事实并持久化更新后的记忆数据。"""
    normalized_content = content.strip()
    if not normalized_content:
        raise ValueError("content")

    normalized_category = category.strip() or "context"
    validated_confidence = _validate_confidence(confidence)
    now = datetime.utcnow().isoformat() + "Z"
    memory_data = get_memory_data(agent_name)
    updated_memory = dict(memory_data)
    facts = list(memory_data.get("facts", []))
    facts.append(
        {
            "id": f"fact_{uuid.uuid4().hex[:8]}",
            "content": normalized_content,
            "category": normalized_category,
            "confidence": validated_confidence,
            "createdAt": now,
            "source": "manual",
        }
    )
    updated_memory["facts"] = facts

    if not _save_memory_to_file(updated_memory, agent_name):
        raise OSError("Failed to save memory data after creating fact")

    return updated_memory


def delete_memory_fact(fact_id: str, agent_name: str | None = None) -> dict[str, Any]:
    """根据 ID 删除事实并持久化更新后的记忆数据。"""
    memory_data = get_memory_data(agent_name)
    facts = memory_data.get("facts", [])
    updated_facts = [fact for fact in facts if fact.get("id") != fact_id]
    if len(updated_facts) == len(facts):
        raise KeyError(fact_id)

    updated_memory = dict(memory_data)
    updated_memory["facts"] = updated_facts

    if not _save_memory_to_file(updated_memory, agent_name):
        raise OSError(f"Failed to save memory data after deleting fact '{fact_id}'")

    return updated_memory


def update_memory_fact(
    fact_id: str,
    content: str | None = None,
    category: str | None = None,
    confidence: float | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """更新已存在的事实并持久化更新后的记忆数据。"""
    memory_data = get_memory_data(agent_name)
    updated_memory = dict(memory_data)
    updated_facts: list[dict[str, Any]] = []
    found = False

    for fact in memory_data.get("facts", []):
        if fact.get("id") == fact_id:
            found = True
            updated_fact = dict(fact)
            if content is not None:
                normalized_content = content.strip()
                if not normalized_content:
                    raise ValueError("content")
                updated_fact["content"] = normalized_content
            if category is not None:
                updated_fact["category"] = category.strip() or "context"
            if confidence is not None:
                updated_fact["confidence"] = _validate_confidence(confidence)
            updated_facts.append(updated_fact)
        else:
            updated_facts.append(fact)

    if not found:
        raise KeyError(fact_id)

    updated_memory["facts"] = updated_facts

    if not _save_memory_to_file(updated_memory, agent_name):
        raise OSError(f"Failed to save memory data after updating fact '{fact_id}'")

    return updated_memory


def _extract_text(content: Any) -> str:
    """从 LLM 响应内容中提取纯文本（支持字符串或内容块列表）。

    现代 LLM 可能返回结构化内容块列表而非纯字符串，
    例如 [{"type": "text", "text": "..."}]。对此类内容使用 str()
    会产生 Python repr 而非实际文本，导致下游 JSON 解析失败。

    字符串片段不带分隔符连接以避免损坏分块 JSON/文本。
    基于字典的文本块视为完整文本块，用换行符连接以提高可读性。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        pending_str_parts: list[str] = []

        def flush_pending_str_parts() -> None:
            if pending_str_parts:
                pieces.append("".join(pending_str_parts))
                pending_str_parts.clear()

        for block in content:
            if isinstance(block, str):
                pending_str_parts.append(block)
            elif isinstance(block, dict):
                flush_pending_str_parts()
                text_val = block.get("text")
                if isinstance(text_val, str):
                    pieces.append(text_val)

        flush_pending_str_parts()
        return "\n".join(pieces)
    return str(content)


# 匹配描述文件上传*事件*而非通用文件相关工作的句子。
# 刻意保持狭窄以避免移除合法事实，如"用户使用 CSV 文件"或"偏好 PDF 导出"。
_UPLOAD_SENTENCE_RE = re.compile(
    r"[^.!?]*\b(?:"
    r"upload(?:ed|ing)?(?:\s+\w+){0,3}\s+(?:file|files?|document|documents?|attachment|attachments?)"
    r"|file\s+upload"
    r"|/mnt/user-data/uploads/"
    r"|<uploaded_files>"
    r")[^.!?]*[.!?]?\s*",
    re.IGNORECASE,
)


def _strip_upload_mentions_from_memory(memory_data: dict[str, Any]) -> dict[str, Any]:
    """从所有记忆摘要和事实中移除关于文件上传的句子。

    上传的文件是会话范围的；将上传事件持久化到长期记忆中
    会导致智能体在后续会话中搜索不存在的文件。
    """
    # Scrub summaries in user/history sections
    for section in ("user", "history"):
        section_data = memory_data.get(section, {})
        for _key, val in section_data.items():
            if isinstance(val, dict) and "summary" in val:
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned

    # Also remove any facts that describe upload events
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [f for f in facts if not _UPLOAD_SENTENCE_RE.search(f.get("content", ""))]

    return memory_data


def _fact_content_key(content: Any) -> str | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    return stripped


class MemoryUpdater:
    """使用 LLM 基于对话上下文更新记忆。"""

    def __init__(self, model_name: str | None = None):
        """初始化记忆更新器。

        Args:
            model_name: 可选的模型名称。如果为 None，使用配置或默认模型。
        """
        self._model_name = model_name

    def _get_model(self):
        """获取用于记忆更新的模型。"""
        config = get_memory_config()
        model_name = self._model_name or config.model_name
        return create_chat_model(name=model_name, thinking_enabled=False)

    def update_memory(self, messages: list[Any], thread_id: str | None = None, agent_name: str | None = None) -> bool:
        """根据对话消息更新记忆。

        Args:
            messages: 对话消息列表。
            thread_id: 可选的线程 ID，用于跟踪来源。
            agent_name: 如果提供，更新按智能体的记忆；如果为 None，更新全局记忆。

        Returns:
            更新成功返回 True，否则返回 False。
        """
        config = get_memory_config()
        if not config.enabled:
            return False

        if not messages:
            return False

        try:
            # Get current memory
            current_memory = get_memory_data(agent_name)

            # Format conversation for prompt
            conversation_text = format_conversation_for_update(messages)

            if not conversation_text.strip():
                return False

            # Build prompt
            prompt = MEMORY_UPDATE_PROMPT.format(
                current_memory=json.dumps(current_memory, indent=2),
                conversation=conversation_text,
            )

            # Call LLM
            model = self._get_model()
            response = model.invoke(prompt)
            response_text = _extract_text(response.content).strip()

            # Parse response
            # Remove markdown code blocks if present
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

            update_data = json.loads(response_text)

            # Apply updates
            updated_memory = self._apply_updates(current_memory, update_data, thread_id)

            # Strip file-upload mentions from all summaries before saving.
            # Uploaded files are session-scoped and won't exist in future sessions,
            # so recording upload events in long-term memory causes the agent to
            # try (and fail) to locate those files in subsequent conversations.
            updated_memory = _strip_upload_mentions_from_memory(updated_memory)

            # Save
            return get_memory_storage().save(updated_memory, agent_name)

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse LLM response for memory update: %s", e)
            return False
        except Exception as e:
            logger.exception("Memory update failed: %s", e)
            return False

    def _apply_updates(
        self,
        current_memory: dict[str, Any],
        update_data: dict[str, Any],
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """将 LLM 生成的更新应用到记忆数据。

        Args:
            current_memory: 当前记忆数据。
            update_data: LLM 生成的更新数据。
            thread_id: 可选的线程 ID，用于跟踪。

        Returns:
            更新后的记忆数据。
        """
        config = get_memory_config()
        now = datetime.utcnow().isoformat() + "Z"

        # Update user sections
        user_updates = update_data.get("user", {})
        for section in ["workContext", "personalContext", "topOfMind"]:
            section_data = user_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["user"][section] = {
                    "summary": section_data["summary"],
                    "updatedAt": now,
                }

        # Update history sections
        history_updates = update_data.get("history", {})
        for section in ["recentMonths", "earlierContext", "longTermBackground"]:
            section_data = history_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["history"][section] = {
                    "summary": section_data["summary"],
                    "updatedAt": now,
                }

        # Remove facts
        facts_to_remove = set(update_data.get("factsToRemove", []))
        if facts_to_remove:
            current_memory["facts"] = [f for f in current_memory.get("facts", []) if f.get("id") not in facts_to_remove]

        # Add new facts
        existing_fact_keys = {fact_key for fact_key in (_fact_content_key(fact.get("content")) for fact in current_memory.get("facts", [])) if fact_key is not None}
        new_facts = update_data.get("newFacts", [])
        for fact in new_facts:
            confidence = fact.get("confidence", 0.5)
            if confidence >= config.fact_confidence_threshold:
                raw_content = fact.get("content", "")
                normalized_content = raw_content.strip()
                fact_key = _fact_content_key(normalized_content)
                if fact_key is not None and fact_key in existing_fact_keys:
                    continue

                fact_entry = {
                    "id": f"fact_{uuid.uuid4().hex[:8]}",
                    "content": normalized_content,
                    "category": fact.get("category", "context"),
                    "confidence": confidence,
                    "createdAt": now,
                    "source": thread_id or "unknown",
                }
                current_memory["facts"].append(fact_entry)
                if fact_key is not None:
                    existing_fact_keys.add(fact_key)

        # Enforce max facts limit
        if len(current_memory["facts"]) > config.max_facts:
            # Sort by confidence and keep top ones
            current_memory["facts"] = sorted(
                current_memory["facts"],
                key=lambda f: f.get("confidence", 0),
                reverse=True,
            )[: config.max_facts]

        return current_memory


def update_memory_from_conversation(messages: list[Any], thread_id: str | None = None, agent_name: str | None = None) -> bool:
    """从对话更新记忆的便捷函数。

    Args:
        messages: 对话消息列表。
        thread_id: 可选的线程 ID。
        agent_name: 如果提供，更新按智能体的记忆；如果为 None，更新全局记忆。

    Returns:
        成功返回 True，否则返回 False。
    """
    updater = MemoryUpdater()
    return updater.update_memory(messages, thread_id, agent_name)
