"""
记忆更新器（第 3 层：提取）

本模块负责从自然语言对话中自动提取用户信息并更新记忆，是记忆系统四层架构中的第 3 层。

核心流程：
  MemoryUpdater.update_memory()
    1. 加载当前 memory.json → current_memory
    2. format_conversation_for_update(messages) → conversation_text（过滤、截断、去标签）
    3. 构建 MEMORY_UPDATE_PROMPT → model.invoke(prompt)（LLM 分析对话）
    4. 解析 LLM 返回的 JSON → update_data
    5. _apply_updates() → 合并更新到 current_memory
    6. _strip_upload_mentions_from_memory() → 清除上传文件相关内容
    7. storage.save() → 原子写入文件

关键设计决策：
  - LLM 驱动更新：对话中的隐含信息（如 "我在字节做 infra" 隐含工作信息）
    无法通过正则/规则穷举提取，必须依靠 LLM 理解上下文
  - 同步 model.invoke() 而非 async：避免跨事件循环共享 httpx AsyncClient
    连接池导致 crash（issue #2615）
  - 线程池卸载：从 async 上下文调用时，通过 ThreadPoolExecutor 运行同步路径

Facts 去重策略：
  - 按 content.casefold() 比较（大小写不敏感）
  - 新 fact 与已有 fact 内容重复时跳过
  - 超过 max_facts (100) 时按 confidence 截断

依赖关系：
  - prompt.py：MEMORY_UPDATE_PROMPT + format_conversation_for_update()
  - storage.py：get_memory_storage().load() / .save()
  - memory_config.py：fact_confidence_threshold、max_facts、model_name
  - models.create_chat_model()：创建 LLM 实例
"""

import asyncio
import atexit
import concurrent.futures
import copy
import json
import logging
import math
import re
import uuid
from typing import Any

from deerflow.agents.memory.prompt import (
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
)
from deerflow.agents.memory.storage import (
    create_empty_memory,
    get_memory_storage,
    utc_now_iso_z,
)
from deerflow.config.memory_config import get_memory_config
from deerflow.models import create_chat_model

logger = logging.getLogger(__name__)


# 后台线程池：用于在 async 上下文中卸载同步的 model.invoke() 调用
# 使用同步调用而非 asyncio.run()，确保不会创建新的事件循环，
# 也不会触碰 langchain 全局缓存的 httpx AsyncClient 连接池
# （该连接池与主 Agent 共享，跨循环复用会导致 crash，见 issue #2615）
_SYNC_MEMORY_UPDATER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="memory-updater-sync",
)
atexit.register(lambda: _SYNC_MEMORY_UPDATER_EXECUTOR.shutdown(wait=False))


# ---- 向后兼容的存储操作包装 ----


def _create_empty_memory() -> dict[str, Any]:
    """向后兼容的空白记忆创建函数（委托给 storage 层）。"""
    return create_empty_memory()


def _save_memory_to_file(memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
    """向后兼容的存储保存函数（委托给配置的存储实现）。"""
    return get_memory_storage().save(memory_data, agent_name, user_id=user_id)


def get_memory_data(agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    """获取当前记忆数据（通过存储提供者）。"""
    return get_memory_storage().load(agent_name, user_id=user_id)


def reload_memory_data(agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    """强制重新加载记忆数据（忽略缓存）。"""
    return get_memory_storage().reload(agent_name, user_id=user_id)


def import_memory_data(memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    """持久化导入的记忆数据。

    用于 API 等外部场景直接导入完整记忆。
    保存后重新加载以获取存储层规范化后的数据。

    Args:
        memory_data: 完整的记忆数据负载
        agent_name: 智能体名称（按智能体隔离时使用）
        user_id: 用户 ID（按用户隔离时使用）

    Returns:
        保存并重新加载后的记忆数据

    Raises:
        OSError: 保存失败时抛出
    """
    storage = get_memory_storage()
    if not storage.save(memory_data, agent_name, user_id=user_id):
        raise OSError("Failed to save imported memory data")
    return storage.load(agent_name, user_id=user_id)


def clear_memory_data(agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    """清除所有存储的记忆数据，恢复为空白结构。"""
    cleared_memory = create_empty_memory()
    if not _save_memory_to_file(cleared_memory, agent_name, user_id=user_id):
        raise OSError("Failed to save cleared memory data")
    return cleared_memory


# ---- Fact CRUD 操作 ----


def _validate_confidence(confidence: float) -> float:
    """校验置信度值是否为 [0, 1] 范围内的有限浮点数。

    确保 JSON 中存储的 confidence 符合标准，非法值抛出 ValueError。
    """
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise ValueError("confidence")
    return confidence


def create_memory_fact(
    content: str,
    category: str = "context",
    confidence: float = 0.5,
    agent_name: str | None = None,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """手动创建一条新 fact 并持久化到记忆文件。

    用于 API 等场景直接添加事实，而非通过 LLM 提取。

    Args:
        content: 事实内容
        category: 分类（preference/knowledge/context/behavior/goal/correction）
        confidence: 置信度 [0, 1]
        agent_name: 智能体名称
        user_id: 用户 ID

    Returns:
        更新后的完整记忆数据
    """
    normalized_content = content.strip()
    if not normalized_content:
        raise ValueError("content")

    normalized_category = category.strip() or "context"
    validated_confidence = _validate_confidence(confidence)
    now = utc_now_iso_z()
    memory_data = get_memory_data(agent_name, user_id=user_id)
    updated_memory = dict(memory_data)
    facts = list(memory_data.get("facts", []))
    facts.append(
        {
            "id": f"fact_{uuid.uuid4().hex[:8]}",
            "content": normalized_content,
            "category": normalized_category,
            "confidence": validated_confidence,
            "createdAt": now,
            "source": "manual",  # 手动创建的 fact 标记来源为 "manual"
        }
    )
    updated_memory["facts"] = facts

    if not _save_memory_to_file(updated_memory, agent_name, user_id=user_id):
        raise OSError("Failed to save memory data after creating fact")

    return updated_memory


def delete_memory_fact(fact_id: str, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
    """按 ID 删除一条 fact 并持久化。

    Args:
        fact_id: 要删除的 fact 的 ID（格式：fact_xxxxxxxx）

    Returns:
        更新后的完整记忆数据

    Raises:
        KeyError: fact_id 不存在时抛出
    """
    memory_data = get_memory_data(agent_name, user_id=user_id)
    facts = memory_data.get("facts", [])
    updated_facts = [fact for fact in facts if fact.get("id") != fact_id]
    if len(updated_facts) == len(facts):
        raise KeyError(fact_id)

    updated_memory = dict(memory_data)
    updated_memory["facts"] = updated_facts

    if not _save_memory_to_file(updated_memory, agent_name, user_id=user_id):
        raise OSError(f"Failed to save memory data after deleting fact '{fact_id}'")

    return updated_memory


def update_memory_fact(
    fact_id: str,
    content: str | None = None,
    category: str | None = None,
    confidence: float | None = None,
    agent_name: str | None = None,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """更新一条已有 fact 的字段并持久化。

    只更新传入的非 None 字段，未传入的字段保持不变。

    Args:
        fact_id: 要更新的 fact 的 ID
        content: 新内容（None 表示不更新）
        category: 新分类（None 表示不更新）
        confidence: 新置信度（None 表示不更新）

    Returns:
        更新后的完整记忆数据

    Raises:
        KeyError: fact_id 不存在时抛出
    """
    memory_data = get_memory_data(agent_name, user_id=user_id)
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

    if not _save_memory_to_file(updated_memory, agent_name, user_id=user_id):
        raise OSError(f"Failed to save memory data after updating fact '{fact_id}'")

    return updated_memory


# ---- LLM 响应解析辅助 ----


def _extract_text(content: Any) -> str:
    """从 LLM 响应内容中提取纯文本。

    现代 LLM 可能返回结构化内容（list of blocks），如：
    [{"type": "text", "text": "..."}]
    对这种内容直接用 str() 会得到 Python repr 而非实际文本，
    导致下游 JSON 解析失败。

    处理策略：
    - str 类型：直接返回
    - list 类型：字符串片段无分隔拼接，dict 文本块用换行拼接
    - 其他类型：str() 回退
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


# 匹配描述文件上传事件的句子（用于从记忆中清除上传相关内容）
# 刻意设计为窄匹配，避免误删 "User works with CSV files" 等合法 fact
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
    """从所有记忆摘要和 facts 中移除上传文件相关句子。

    原因：上传文件是会话级的（session-scoped），文件路径在下轮对话时不存在。
    如果将上传事件持久化到长期记忆中，Agent 会在后续对话中搜索不存在的文件，造成混淆。

    处理范围：
    - user / history 分区中的 summary 字段：正则移除上传相关句子
    - facts 列表：移除内容匹配上传事件模式的 fact
    """
    # 清理 user/history 分区中的摘要
    for section in ("user", "history"):
        section_data = memory_data.get(section, {})
        for _key, val in section_data.items():
            if isinstance(val, dict) and "summary" in val:
                cleaned = _UPLOAD_SENTENCE_RE.sub("", val["summary"]).strip()
                cleaned = re.sub(r"  +", " ", cleaned)
                val["summary"] = cleaned

    # 移除描述上传事件的 facts
    facts = memory_data.get("facts", [])
    if facts:
        memory_data["facts"] = [f for f in facts if not _UPLOAD_SENTENCE_RE.search(f.get("content", ""))]

    return memory_data


def _fact_content_key(content: Any) -> str | None:
    """生成 fact 内容的去重键（大小写不敏感）。

    返回去除首尾空白后、casefold 处理的字符串。
    用于判断新 fact 是否与已有 fact 内容重复。
    无效内容（非字符串、空字符串）返回 None。
    """
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    return stripped.casefold()


# ---- 记忆更新器核心类 ----


class MemoryUpdater:
    """基于 LLM 的记忆更新器（第 3 层核心类）。

    从对话中提取用户信息，生成结构化更新指令，应用到记忆数据并持久化。
    """

    def __init__(self, model_name: str | None = None):
        """初始化记忆更新器。

        Args:
            model_name: 可选的模型名称。None 则使用 memory_config 中的配置或默认模型。
        """
        self._model_name = model_name

    def _get_model(self):
        """获取用于记忆更新的 LLM 模型实例。

        禁用 thinking（扩展推理），因为记忆更新是结构化 JSON 输出任务，
        不需要链式推理。
        """
        config = get_memory_config()
        model_name = self._model_name or config.model_name
        return create_chat_model(name=model_name, thinking_enabled=False)

    def _build_correction_hint(
        self,
        correction_detected: bool,
        reinforcement_detected: bool,
    ) -> str:
        """构建纠错/正面反馈的提示词附加段落。

        当 message_processing.py 检测到用户发出纠错或正面反馈信号时，
        在 MEMORY_UPDATE_PROMPT 的 {correction_hint} 占位符中注入额外指令，
        要求 LLM 生成高置信度的 correction/preference/behavior 类别 fact。

        - 纠错信号 → 生成 correction 类别 fact（confidence >= 0.95）
        - 正面反馈信号 → 生成 preference/behavior 类别 fact（confidence >= 0.9）
        - 纠错优先级高于正面反馈（两者同时存在时两个提示都会注入）
        """
        correction_hint = ""
        if correction_detected:
            correction_hint = (
                "IMPORTANT: Explicit correction signals were detected in this conversation. "
                "Pay special attention to what the agent got wrong, what the user corrected, "
                "and record the correct approach as a fact with category "
                '"correction" and confidence >= 0.95 when appropriate.'
            )
        if reinforcement_detected:
            reinforcement_hint = (
                "IMPORTANT: Positive reinforcement signals were detected in this conversation. "
                "The user explicitly confirmed the agent's approach was correct or helpful. "
                "Record the confirmed approach, style, or preference as a fact with category "
                '"preference" or "behavior" and confidence >= 0.9 when appropriate.'
            )
            correction_hint = (correction_hint + "\n" + reinforcement_hint).strip() if correction_hint else reinforcement_hint

        return correction_hint

    def _prepare_update_prompt(
        self,
        messages: list[Any],
        agent_name: str | None,
        correction_detected: bool,
        reinforcement_detected: bool,
        user_id: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        """准备 LLM 更新提示词。

        流程：
        1. 检查记忆功能是否启用、消息列表是否为空
        2. 加载当前记忆数据
        3. 格式化对话文本
        4. 构建纠错/反馈提示
        5. 组装最终的 MEMORY_UPDATE_PROMPT

        Returns:
            (current_memory, prompt) 元组，或 None（记忆禁用/消息为空/对话为空时）
        """
        config = get_memory_config()
        if not config.enabled or not messages:
            return None

        current_memory = get_memory_data(agent_name, user_id=user_id)
        conversation_text = format_conversation_for_update(messages)
        if not conversation_text.strip():
            return None

        correction_hint = self._build_correction_hint(
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
        )
        prompt = MEMORY_UPDATE_PROMPT.format(
            current_memory=json.dumps(current_memory, indent=2),
            conversation=conversation_text,
            correction_hint=correction_hint,
        )
        return current_memory, prompt

    def _finalize_update(
        self,
        current_memory: dict[str, Any],
        response_content: Any,
        thread_id: str | None,
        agent_name: str | None,
        user_id: str | None = None,
    ) -> bool:
        """解析 LLM 响应、应用更新并持久化记忆。

        流程：
        1. 从 LLM 响应中提取文本（支持 markdown 代码块包裹的 JSON）
        2. 解析 JSON 为更新指令
        3. 深拷贝当前记忆后应用更新（避免原地修改导致缓存问题）
        4. 清除上传文件相关内容
        5. 保存到文件
        """
        response_text = _extract_text(response_content).strip()

        # 去除可能的 markdown 代码块包裹（```json ... ```）
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        update_data = json.loads(response_text)
        # 深拷贝后再原地修改，防止保存失败时破坏缓存中的原始对象引用
        updated_memory = self._apply_updates(copy.deepcopy(current_memory), update_data, thread_id)
        updated_memory = _strip_upload_mentions_from_memory(updated_memory)
        return get_memory_storage().save(updated_memory, agent_name, user_id=user_id)

    async def aupdate_memory(
        self,
        messages: list[Any],
        thread_id: str | None = None,
        agent_name: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
        user_id: str | None = None,
    ) -> bool:
        """异步记忆更新入口，通过 asyncio.to_thread 委托到同步路径。

        使用同步 model.invoke() 而非 async model.ainvoke()，
        避免创建新事件循环，从而不会触碰 langchain 异步 httpx 客户端连接池
        （该连接池与主 Agent 共享，跨循环连接复用会导致 crash，issue #2615）。
        """
        return await asyncio.to_thread(
            self._do_update_memory_sync,
            messages=messages,
            thread_id=thread_id,
            agent_name=agent_name,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
            user_id=user_id,
        )

    def _do_update_memory_sync(
        self,
        messages: list[Any],
        thread_id: str | None = None,
        agent_name: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
        user_id: str | None = None,
    ) -> bool:
        """纯同步的记忆更新路径，使用 model.invoke()。

        保证使用同步 HTTP 连接池，与主 Agent 的异步连接池完全隔离，
        不会出现跨事件循环的连接复用问题。
        """
        try:
            prepared = self._prepare_update_prompt(
                messages=messages,
                agent_name=agent_name,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
                user_id=user_id,
            )
            if prepared is None:
                return False

            current_memory, prompt = prepared
            model = self._get_model()
            response = model.invoke(prompt, config={"run_name": "memory_agent"})
            return self._finalize_update(
                current_memory=current_memory,
                response_content=response.content,
                thread_id=thread_id,
                agent_name=agent_name,
                user_id=user_id,
            )
        except json.JSONDecodeError as e:
            # LLM 返回了无效 JSON，记录警告但不中断
            logger.warning("Failed to parse LLM response for memory update: %s", e)
            return False
        except Exception as e:
            logger.exception("Memory update failed: %s", e)
            return False

    def update_memory(
        self,
        messages: list[Any],
        thread_id: str | None = None,
        agent_name: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
        user_id: str | None = None,
    ) -> bool:
        """同步记忆更新入口（智能调度同步/线程池执行）。

        调用策略：
        - 若当前已在运行中的事件循环内（如从 LangGraph 节点调用）：
          通过 ThreadPoolExecutor 卸载到后台线程，避免阻塞调用方的事件循环
        - 若不在事件循环中：直接同步执行

        两种路径最终都调用 _do_update_memory_sync()，即同步 model.invoke()。

        Args:
            messages: 对话消息列表
            thread_id: 线程 ID（用于追踪 fact 来源）
            agent_name: 智能体名称（按智能体隔离时使用）
            correction_detected: 是否检测到用户纠错信号
            reinforcement_detected: 是否检测到正面反馈信号
            user_id: 用户 ID（按用户隔离时使用）

        Returns:
            True 表示更新成功，False 表示跳过/失败
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # 在事件循环内 → 卸载到线程池
            try:
                future = _SYNC_MEMORY_UPDATER_EXECUTOR.submit(
                    self._do_update_memory_sync,
                    messages=messages,
                    thread_id=thread_id,
                    agent_name=agent_name,
                    correction_detected=correction_detected,
                    reinforcement_detected=reinforcement_detected,
                    user_id=user_id,
                )
                return future.result()
            except Exception:
                logger.exception("Failed to offload memory update to executor")
                return False

        # 不在事件循环中 → 直接同步执行
        return self._do_update_memory_sync(
            messages=messages,
            thread_id=thread_id,
            agent_name=agent_name,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
            user_id=user_id,
        )

    def _apply_updates(
        self,
        current_memory: dict[str, Any],
        update_data: dict[str, Any],
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """将 LLM 返回的 JSON 更新指令应用到记忆数据。

        更新规则：
        1. User sections（workContext/personalContext/topOfMind）：
           shouldUpdate=true 且 summary 非空时才覆盖
        2. History sections（recentMonths/earlierContext/longTermBackground）：同上
        3. factsToRemove：按 ID 删除对应 fact
        4. newFacts：
           - confidence >= fact_confidence_threshold (0.7) 才入库
           - 按 content.casefold() 去重
           - correction 类别可携带 sourceError 字段
        5. 超过 max_facts (100) 时按 confidence 降序截断

        Args:
            current_memory: 当前记忆数据（已被深拷贝）
            update_data: LLM 返回的更新指令
            thread_id: 线程 ID（记录在 fact 的 source 字段中）

        Returns:
            更新后的记忆数据（原地修改 current_memory）
        """
        config = get_memory_config()
        now = utc_now_iso_z()

        # 更新 user 分区
        user_updates = update_data.get("user", {})
        for section in ["workContext", "personalContext", "topOfMind"]:
            section_data = user_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["user"][section] = {
                    "summary": section_data["summary"],
                    "updatedAt": now,
                }

        # 更新 history 分区
        history_updates = update_data.get("history", {})
        for section in ["recentMonths", "earlierContext", "longTermBackground"]:
            section_data = history_updates.get(section, {})
            if section_data.get("shouldUpdate") and section_data.get("summary"):
                current_memory["history"][section] = {
                    "summary": section_data["summary"],
                    "updatedAt": now,
                }

        # 删除指定的 facts
        facts_to_remove = set(update_data.get("factsToRemove", []))
        if facts_to_remove:
            current_memory["facts"] = [f for f in current_memory.get("facts", []) if f.get("id") not in facts_to_remove]

        # 添加新 facts（带去重）
        existing_fact_keys = {fact_key for fact_key in (_fact_content_key(fact.get("content")) for fact in current_memory.get("facts", [])) if fact_key is not None}
        new_facts = update_data.get("newFacts", [])
        for fact in new_facts:
            confidence = fact.get("confidence", 0.5)
            if confidence >= config.fact_confidence_threshold:
                raw_content = fact.get("content", "")
                if not isinstance(raw_content, str):
                    continue
                normalized_content = raw_content.strip()
                fact_key = _fact_content_key(normalized_content)
                # 按 casefold 去重：已存在相同内容的 fact 则跳过
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
                # correction 类别携带 sourceError 字段
                source_error = fact.get("sourceError")
                if isinstance(source_error, str):
                    normalized_source_error = source_error.strip()
                    if normalized_source_error:
                        fact_entry["sourceError"] = normalized_source_error
                current_memory["facts"].append(fact_entry)
                if fact_key is not None:
                    existing_fact_keys.add(fact_key)

        # 强制执行 max_facts 上限：按置信度降序排列，截断到上限
        if len(current_memory["facts"]) > config.max_facts:
            current_memory["facts"] = sorted(
                current_memory["facts"],
                key=lambda f: f.get("confidence", 0),
                reverse=True,
            )[: config.max_facts]

        return current_memory


# ---- 便捷函数 ----


def update_memory_from_conversation(
    messages: list[Any],
    thread_id: str | None = None,
    agent_name: str | None = None,
    correction_detected: bool = False,
    reinforcement_detected: bool = False,
    user_id: str | None = None,
) -> bool:
    """便捷函数：从对话更新记忆。

    创建 MemoryUpdater 实例并调用 update_memory()。
    用于中间件等调用方简化代码。

    Args:
        messages: 对话消息列表
        thread_id: 线程 ID
        agent_name: 智能体名称
        correction_detected: 是否检测到纠错信号
        reinforcement_detected: 是否检测到正面反馈信号
        user_id: 用户 ID

    Returns:
        True 表示更新成功，False 表示跳过/失败
    """
    updater = MemoryUpdater()
    return updater.update_memory(messages, thread_id, agent_name, correction_detected, reinforcement_detected, user_id=user_id)
