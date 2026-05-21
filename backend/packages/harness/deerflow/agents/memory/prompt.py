"""
记忆系统的提示词模板与注入格式化（第 1 层 + 第 3 层）

本模块承担记忆系统中的两个职责：
1. 第 1 层（注入）：format_memory_for_injection() 将记忆格式化后注入 system prompt
2. 第 3 层（提取）：MEMORY_UPDATE_PROMPT 指导 LLM 从对话中提取记忆更新

关键组件：
- MEMORY_UPDATE_PROMPT：~120 行的详细提示词，指导 LLM 分析对话并返回 JSON 更新指令
- FACT_EXTRACTION_PROMPT：从单条消息中提取事实的提示词
- format_memory_for_injection()：按置信度排序 facts → token 预算截断 → 格式化为文本
- format_conversation_for_update()：将对话消息格式化为文本供更新提示词使用

注入策略（第 1 层）：
- 使用 tiktoken 精确计算 token 数（cl100k_base 编码）
- Facts 按 confidence 降序排列，低置信度在 token 不足时被裁剪
- correction 类别特殊处理：追加 "(avoid: sourceError)" 标记
- 默认 token 预算为 2000（max_injection_tokens 配置）

依赖：
- tiktoken（可选）：精确 token 计数，未安装时回退到字符数 ÷ 4 估算
- memory_config.py：max_injection_tokens 等配置
"""

import math
import re
from typing import Any

try:
    import tiktoken

    TIKTOKEN_AVAILABLE = True
except ImportError:
    # tiktoken 是可选依赖，未安装时回退到字符估算
    TIKTOKEN_AVAILABLE = False

# ---- 提示词模板 ----

# 第 3 层使用的更新提示词（~120 行）
# 输入变量：{current_memory}、{conversation}、{correction_hint}
# 输出格式：JSON，包含 user/history sections 的更新 + newFacts + factsToRemove
MEMORY_UPDATE_PROMPT = """You are a memory management system. Your task is to analyze a conversation and update the user's memory profile.

Current Memory State:
<current_memory>
{current_memory}
</current_memory>

New Conversation to Process:
<conversation>
{conversation}
</conversation>

Instructions:
1. Analyze the conversation for important information about the user
2. Extract relevant facts, preferences, and context with specific details (numbers, names, technologies)
3. Update the memory sections as needed following the detailed length guidelines below

Before extracting facts, perform a structured reflection on the conversation:
1. Error/Retry Detection: Did the agent encounter errors, require retries, or produce incorrect results?
   If yes, record the root cause and correct approach as a high-confidence fact with category "correction".
2. User Correction Detection: Did the user correct the agent's direction, understanding, or output?
   If yes, record the correct interpretation or approach as a high-confidence fact with category "correction".
   Include what went wrong in "sourceError" only when category is "correction" and the mistake is explicit in the conversation.
3. Project Constraint Discovery: Were any project-specific constraints discovered during the conversation?
   If yes, record them as facts with the most appropriate category and confidence.

{correction_hint}

Memory Section Guidelines:

**User Context** (Current state - concise summaries):
- workContext: Professional role, company, key projects, main technologies (2-3 sentences)
  Example: Core contributor, project names with metrics (16k+ stars), technical stack
- personalContext: Languages, communication preferences, key interests (1-2 sentences)
  Example: Bilingual capabilities, specific interest areas, expertise domains
- topOfMind: Multiple ongoing focus areas and priorities (3-5 sentences, detailed paragraph)
  Example: Primary project work, parallel technical investigations, ongoing learning/tracking
  Include: Active implementation work, troubleshooting issues, market/research interests
  Note: This captures SEVERAL concurrent focus areas, not just one task

**History** (Temporal context - rich paragraphs):
- recentMonths: Detailed summary of recent activities (4-6 sentences or 1-2 paragraphs)
  Timeline: Last 1-3 months of interactions
  Include: Technologies explored, projects worked on, problems solved, interests demonstrated
- earlierContext: Important historical patterns (3-5 sentences or 1 paragraph)
  Timeline: 3-12 months ago
  Include: Past projects, learning journeys, established patterns
- longTermBackground: Persistent background and foundational context (2-4 sentences)
  Timeline: Overall/foundational information
  Include: Core expertise, longstanding interests, fundamental working style

**Facts Extraction**:
- Extract specific, quantifiable details (e.g., "16k+ GitHub stars", "200+ datasets")
- Include proper nouns (company names, project names, technology names)
- Preserve technical terminology and version numbers
- Categories:
  * preference: Tools, styles, approaches user prefers/dislikes
  * knowledge: Specific expertise, technologies mastered, domain knowledge
  * context: Background facts (job title, projects, locations, languages)
  * behavior: Working patterns, communication habits, problem-solving approaches
  * goal: Stated objectives, learning targets, project ambitions
  * correction: Explicit agent mistakes or user corrections, including the correct approach
- Confidence levels:
  * 0.9-1.0: Explicitly stated facts ("I work on X", "My role is Y")
  * 0.7-0.8: Strongly implied from actions/discussions
  * 0.5-0.6: Inferred patterns (use sparingly, only for clear patterns)

**What Goes Where**:
- workContext: Current job, active projects, primary tech stack
- personalContext: Languages, personality, interests outside direct work tasks
- topOfMind: Multiple ongoing priorities and focus areas user cares about recently (gets updated most frequently)
  Should capture 3-5 concurrent themes: main work, side explorations, learning/tracking interests
- recentMonths: Detailed account of recent technical explorations and work
- earlierContext: Patterns from slightly older interactions still relevant
- longTermBackground: Unchanging foundational facts about the user

**Multilingual Content**:
- Preserve original language for proper nouns and company names
- Keep technical terms in their original form (DeepSeek, LangGraph, etc.)
- Note language capabilities in personalContext

Output Format (JSON):
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "history": {{
    "recentMonths": {{ "summary": "...", "shouldUpdate": true/false }},
    "earlierContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "longTermBackground": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "newFacts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0 }}
  ],
  "factsToRemove": ["fact_id_1", "fact_id_2"]
}}

Important Rules:
- Only set shouldUpdate=true if there's meaningful new information
- Follow length guidelines: workContext/personalContext are concise (1-3 sentences), topOfMind and history sections are detailed (paragraphs)
- Include specific metrics, version numbers, and proper nouns in facts
- Only add facts that are clearly stated (0.9+) or strongly implied (0.7+)
- Use category "correction" for explicit agent mistakes or user corrections; assign confidence >= 0.95 when the correction is explicit
- Include "sourceError" only for explicit correction facts when the prior mistake or wrong approach is clearly stated; omit it otherwise
- Remove facts that are contradicted by new information
- When updating topOfMind, integrate new focus areas while removing completed/abandoned ones
  Keep 3-5 concurrent focus themes that are still active and relevant
- For history sections, integrate new information chronologically into appropriate time period
- Preserve technical accuracy - keep exact names of technologies, companies, projects
- Focus on information useful for future interactions and personalization
- IMPORTANT: Do NOT record file upload events in memory. Uploaded files are
  session-specific and ephemeral — they will not be accessible in future sessions.
  Recording upload events causes confusion in subsequent conversations.

Return ONLY valid JSON, no explanation or markdown."""


# 从单条消息中提取事实的提示词（轻量级，用于 API 等场景）
FACT_EXTRACTION_PROMPT = """Extract factual information about the user from this message.

Message:
{message}

Extract facts in this JSON format:
{{
  "facts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0 }}
  ]
}}

Categories:
- preference: User preferences (likes/dislikes, styles, tools)
- knowledge: User's expertise or knowledge areas
- context: Background context (location, job, projects)
- behavior: Behavioral patterns
- goal: User's goals or objectives
- correction: Explicit corrections or mistakes to avoid repeating

Rules:
- Only extract clear, specific facts
- Confidence should reflect certainty (explicit statement = 0.9+, implied = 0.6-0.8)
- Skip vague or temporary information

Return ONLY valid JSON."""


# ---- 工具函数 ----


def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """计算文本的 token 数量。

    优先使用 tiktoken 精确计数（cl100k_base 编码，GPT-4/3.5 使用）。
    tiktoken 未安装或出错时回退到字符数 ÷ 4 的粗略估算。

    Args:
        text: 待计算 token 的文本
        encoding_name: tiktoken 编码名称，默认 cl100k_base

    Returns:
        token 数量
    """
    if not TIKTOKEN_AVAILABLE:
        # tiktoken 未安装，回退到字符估算（平均每个 token 约 4 个字符）
        return len(text) // 4

    try:
        encoding = tiktoken.get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception:
        # 编码加载失败，同样回退
        return len(text) // 4


def _coerce_confidence(value: Any, default: float = 0.0) -> float:
    """将置信度值安全转换为 [0, 1] 范围内的浮点数。

    处理以下异常情况：
    - None / 非数字类型 → 回退到 default
    - NaN / Inf / -Inf → 回退到 default（防止非法值主导排序）
    - 正常数值 → 钳制到 [0, 1] 区间

    Args:
        value: 原始置信度值（可能是任意类型）
        default: 非法值时的默认回退值（假定有限）

    Returns:
        [0, 1] 范围内的浮点数
    """
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, default))
    if not math.isfinite(confidence):
        return max(0.0, min(1.0, default))
    return max(0.0, min(1.0, confidence))


# ---- 第 1 层：注入格式化 ----


def format_memory_for_injection(memory_data: dict[str, Any], max_tokens: int = 2000) -> str:
    """将记忆数据格式化为可注入 system prompt 的文本（第 1 层核心函数）。

    格式化流程：
    1. User Context → "Work: ... / Personal: ... / Current Focus: ..."
    2. History → "Recent: ... / Earlier: ... / Background: ..."
    3. Facts → 按 confidence 降序排列
       - 逐条加入，每加一条用 tiktoken 实时算 token
       - 超出 max_tokens 预算时停止
       - correction 类别特殊格式：追加 "(avoid: sourceError)"

    注入时机：Agent 构建时（make_lead_agent → _get_memory_context），
    不是运行时。本轮更新的记忆，下一轮才能看到。

    Args:
        memory_data: 从 storage 加载的记忆数据字典
        max_tokens: 最大 token 预算（默认 2000）

    Returns:
        格式化后的记忆文本，用于注入 <memory> XML 块；无内容时返回空字符串
    """
    if not memory_data:
        return ""

    sections = []

    # 格式化 User Context 部分（工作、个人、当前关注点）
    user_data = memory_data.get("user", {})
    if user_data:
        user_sections = []

        work_ctx = user_data.get("workContext", {})
        if work_ctx.get("summary"):
            user_sections.append(f"Work: {work_ctx['summary']}")

        personal_ctx = user_data.get("personalContext", {})
        if personal_ctx.get("summary"):
            user_sections.append(f"Personal: {personal_ctx['summary']}")

        top_of_mind = user_data.get("topOfMind", {})
        if top_of_mind.get("summary"):
            user_sections.append(f"Current Focus: {top_of_mind['summary']}")

        if user_sections:
            sections.append("User Context:\n" + "\n".join(f"- {s}" for s in user_sections))

    # 格式化 History 部分（近期、早期、长期背景）
    history_data = memory_data.get("history", {})
    if history_data:
        history_sections = []

        recent = history_data.get("recentMonths", {})
        if recent.get("summary"):
            history_sections.append(f"Recent: {recent['summary']}")

        earlier = history_data.get("earlierContext", {})
        if earlier.get("summary"):
            history_sections.append(f"Earlier: {earlier['summary']}")

        background = history_data.get("longTermBackground", {})
        if background.get("summary"):
            history_sections.append(f"Background: {background['summary']}")

        if history_sections:
            sections.append("History:\n" + "\n".join(f"- {s}" for s in history_sections))

    # 格式化 Facts 部分（按置信度排序，受 token 预算限制）
    facts_data = memory_data.get("facts", [])
    if isinstance(facts_data, list) and facts_data:
        # 按 confidence 降序排列，过滤掉无效的 fact 条目
        ranked_facts = sorted(
            (f for f in facts_data if isinstance(f, dict) and isinstance(f.get("content"), str) and f.get("content").strip()),
            key=lambda fact: _coerce_confidence(fact.get("confidence"), default=0.0),
            reverse=True,
        )

        # 先计算已有 sections 的 token 数，再逐条加入 facts
        base_text = "\n\n".join(sections)
        base_tokens = _count_tokens(base_text) if base_text else 0
        # 预留 "Facts:\n" 标题和分隔符的 token
        facts_header = "Facts:\n"
        separator_tokens = _count_tokens("\n\n" + facts_header) if base_text else _count_tokens(facts_header)
        running_tokens = base_tokens + separator_tokens

        fact_lines: list[str] = []
        for fact in ranked_facts:
            content_value = fact.get("content")
            if not isinstance(content_value, str):
                continue
            content = content_value.strip()
            if not content:
                continue
            category = str(fact.get("category", "context")).strip() or "context"
            confidence = _coerce_confidence(fact.get("confidence"), default=0.0)
            source_error = fact.get("sourceError")
            # correction 类别特殊格式：显示应避免的错误
            if category == "correction" and isinstance(source_error, str) and source_error.strip():
                line = f"- [{category} | {confidence:.2f}] {content} (avoid: {source_error.strip()})"
            else:
                line = f"- [{category} | {confidence:.2f}] {content}"

            # 增量计算 token，超预算则停止
            line_text = ("\n" + line) if fact_lines else line
            line_tokens = _count_tokens(line_text)

            if running_tokens + line_tokens <= max_tokens:
                fact_lines.append(line)
                running_tokens += line_tokens
            else:
                break

        if fact_lines:
            sections.append("Facts:\n" + "\n".join(fact_lines))

    if not sections:
        return ""

    result = "\n\n".join(sections)

    # 最终安全检查：如果格式化后的文本仍超过 token 限制，按比例截断
    token_count = _count_tokens(result)
    if token_count > max_tokens:
        # 根据字符/token 比率估算需要截断的字符数
        char_per_token = len(result) / token_count
        target_chars = int(max_tokens * char_per_token * 0.95)  # 95% 留出安全余量
        result = result[:target_chars] + "\n..."

    return result


# ---- 第 3 层：对话格式化 ----


def format_conversation_for_update(messages: list[Any]) -> str:
    """将对话消息列表格式化为文本，供 MEMORY_UPDATE_PROMPT 使用。

    格式化规则：
    1. 只保留 human 和 ai 消息（过滤掉 system、tool 等类型）
    2. human 消息中的 <uploaded_files> 标签被正则移除（上传路径是会话级的，不应持久化）
    3. 若移除上传标签后消息为空，跳过该条消息
    4. 超过 1000 字的消息被截断（防止过长消息消耗过多 token）
    5. 支持多模态内容（content 为 list 类型时提取文本部分）

    Args:
        messages: 对话消息列表（LangChain Message 对象）

    Returns:
        格式化后的对话文本（"User: ...\n\nAssistant: ..." 格式）
    """
    lines = []
    for msg in messages:
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", str(msg))

        # 处理多模态内容（content 可能是 list 而非 str）
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    text_val = p.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            content = " ".join(text_parts) if text_parts else str(content)

        # 移除 human 消息中的 <uploaded_files> 块
        # 上传文件路径是会话级的，不应写入长期记忆
        if role == "human":
            content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", str(content)).strip()
            if not content:
                continue

        # 截断过长消息（> 1000 字）
        if len(str(content)) > 1000:
            content = str(content)[:1000] + "..."

        if role == "human":
            lines.append(f"User: {content}")
        elif role == "ai":
            lines.append(f"Assistant: {content}")

    return "\n\n".join(lines)
