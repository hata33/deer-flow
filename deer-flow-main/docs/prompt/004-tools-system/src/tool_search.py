"""延迟工具搜索 — 运行时按需发现工具。

包含：
- DeferredToolRegistry：存储延迟加载的工具，支持正则表达式搜索
- tool_search：LangChain 工具，供 agent 调用以发现延迟工具的完整定义

agent 在系统提示的 <available-deferred-tools> 中可以看到延迟工具的名称，
但无法直接调用，必须通过 tool_search 获取完整的参数定义后才能使用。
设计上与工具来源无关（不涉及 MCP 等具体来源）。
"""

import contextvars
import json
import logging
import re
from dataclasses import dataclass

from langchain.tools import BaseTool
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_function

logger = logging.getLogger(__name__)

MAX_RESULTS = 5  # 每次搜索最多返回的工具数量


# ── 注册表 ──


@dataclass
class DeferredToolEntry:
    """延迟工具的轻量元数据条目（不含完整参数定义，减少上下文占用）。"""

    name: str
    description: str
    tool: BaseTool  # Full tool object, returned only on search match


class DeferredToolRegistry:
    """延迟工具注册表，支持通过正则表达式按名称和描述搜索。"""

    def __init__(self):
        self._entries: list[DeferredToolEntry] = []

    def register(self, tool: BaseTool) -> None:
        self._entries.append(
            DeferredToolEntry(
                name=tool.name,
                description=tool.description or "",
                tool=tool,
            )
        )

    def promote(self, names: set[str]) -> None:
        """将工具从延迟注册表中提升为活跃状态。

        在 tool_search 返回工具的完整定义后调用。
        LLM 已获取完整参数定义，DeferredToolFilterMiddleware 将不再
        在后续的 bind_tools 调用中过滤这些工具。
        """
        if not names:
            return
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.name not in names]
        promoted = before - len(self._entries)
        if promoted:
            logger.debug(f"Promoted {promoted} tool(s) from deferred to active: {names}")

    def search(self, query: str) -> list[BaseTool]:
        """Search deferred tools by regex pattern against name + description.

        Supports three query forms (aligned with Claude Code):
          - "select:name1,name2" — exact name match
          - "+keyword rest" — name must contain keyword, rank by rest
          - "keyword query" — regex match against name + description

        Returns:
            List of matched BaseTool objects (up to MAX_RESULTS).
        """
        if query.startswith("select:"):
            # 精确选择模式："select:name1,name2"
            names = {n.strip() for n in query[7:].split(",")}
            return [e.tool for e in self._entries if e.name in names][:MAX_RESULTS]

        if query.startswith("+"):
            # 必须包含模式："+keyword rest" — 名称必须包含 keyword，按 rest 排序
            parts = query[1:].split(None, 1)
            required = parts[0].lower()
            candidates = [e for e in self._entries if required in e.name.lower()]
            if len(parts) > 1:
                candidates.sort(
                    key=lambda e: _regex_score(parts[1], e),
                    reverse=True,
                )
            return [e.tool for e in candidates][:MAX_RESULTS]

        # 通用正则搜索模式
        try:
            regex = re.compile(query, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(query), re.IGNORECASE)

        scored = []
        for entry in self._entries:
            searchable = f"{entry.name} {entry.description}"
            if regex.search(searchable):
                # 名称匹配优先级（2）高于描述匹配（1）
                score = 2 if regex.search(entry.name) else 1
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry.tool for _, entry in scored][:MAX_RESULTS]

    @property
    def entries(self) -> list[DeferredToolEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def _regex_score(pattern: str, entry: DeferredToolEntry) -> int:
    """计算正则匹配得分，用于候选工具排序。"""
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    return len(regex.findall(f"{entry.name} {entry.description}"))


# ── 每请求注册表（ContextVar）──
#
# 使用 ContextVar 而非模块级全局变量，防止并发请求之间互相干扰。
# 在基于 asyncio 的 LangGraph 中，每个图执行在独立的异步上下文中运行，
# 因此每个请求拥有独立的注册表值。对于通过 loop.run_in_executor
# 运行的同步工具，Python 会将当前上下文复制到工作线程，
# 因此 ContextVar 值也能正确继承。

_registry_var: contextvars.ContextVar[DeferredToolRegistry | None] = contextvars.ContextVar("deferred_tool_registry", default=None)


def get_deferred_registry() -> DeferredToolRegistry | None:
    return _registry_var.get()


def set_deferred_registry(registry: DeferredToolRegistry) -> None:
    _registry_var.set(registry)


def reset_deferred_registry() -> None:
    """重置当前异步上下文中的延迟注册表。"""
    _registry_var.set(None)


# ── 工具定义 ──


@tool
def tool_search(query: str) -> str:
    """Fetches full schema definitions for deferred tools so they can be called.

    Deferred tools appear by name in <available-deferred-tools> in the system
    prompt. Until fetched, only the name is known — there is no parameter
    schema, so the tool cannot be invoked. This tool takes a query, matches
    it against the deferred tool list, and returns the matched tools' complete
    definitions. Once a tool's schema appears in that result, it is callable.

    Query forms:
      - "select:Read,Edit,Grep" — fetch these exact tools by name
      - "notebook jupyter" — keyword search, up to max_results best matches
      - "+slack send" — require "slack" in the name, rank by remaining terms

    Args:
        query: Query to find deferred tools. Use "select:<tool_name>" for
               direct selection, or keywords to search.

    Returns:
        Matched tool definitions as JSON array.
    """
    registry = get_deferred_registry()
    if not registry:
        return "No deferred tools available."

    matched_tools = registry.search(query)
    if not matched_tools:
        return f"No tools found matching: {query}"

    # 使用 LangChain 内置序列化生成 OpenAI function 格式（模型无关的标准定义）
    tool_defs = [convert_to_openai_function(t) for t in matched_tools[:MAX_RESULTS]]

    # 将匹配的工具提升为活跃状态，后续 DeferredToolFilterMiddleware 不再过滤它们
    registry.promote({t.name for t in matched_tools[:MAX_RESULTS]})

    return json.dumps(tool_defs, indent=2, ensure_ascii=False)
