"""延迟工具搜索（Deferred Tool Search）

本模块实现了延迟工具发现机制，允许代理按需发现和加载工具，而非一次性加载所有工具。

核心概念：
--------
**延迟工具**（Deferred Tool）是指工具名称已告知代理，但完整的参数 schema 尚未暴露的工具。
代理在系统提示的 `<available-deferred-tools>` 中可以看到这些工具的名称，
但无法调用它们——直到通过 `tool_search` 工具获取完整的 schema 定义。

为什么需要延迟加载？
------------------
1. **减少上下文占用**：大量 MCP 工具的完整 schema 会占用宝贵的上下文窗口
2. **按需加载**：代理只在需要时才加载特定工具的详细定义
3. **提高效率**：减少每次模型调用时需要处理的 token 数量

延迟工具注册表（DeferredToolRegistry）：
--------------------------------------
- 存储所有延迟工具的元数据（名称、描述、完整工具对象）
- 支持三种搜索模式：
  1. `select:name1,name2` — 按名称精确匹配
  2. `+keyword rest` — 名称必须包含 keyword，按剩余关键词排序
  3. `keyword query` — 正则匹配名称和描述

工具提升（Promotion）机制：
------------------------
当 `tool_search` 返回某个工具的 schema 后：
1. 该工具从注册表中移除（promote）
2. `DeferredToolFilterMiddleware` 不再从 bind_tools 中过滤它
3. 代理在后续模型调用中获得完整的工具定义并可以调用

请求级隔离（ContextVar）：
-----------------------
使用 `contextvars.ContextVar` 存储注册表实例，确保：
- 每个异步请求有独立的注册表（防止并发请求互相干扰）
- 子代理重入调用时复用父代理的注册表（保留已提升的工具）
- 同步工具通过 loop.run_in_executor 执行时正确继承 ContextVar

模块结构：
--------
- `DeferredToolEntry`：延迟工具条目的轻量级元数据
- `DeferredToolRegistry`：延迟工具注册表，支持正则搜索
- `tool_search`：代理可调用的工具搜索 LangChain 工具
- `get/set/reset_deferred_registry`：注册表的 ContextVar 访问器
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

# 每次搜索返回的最大工具数量
MAX_RESULTS = 5


# ── 注册表 ──


@dataclass
class DeferredToolEntry:
    """延迟工具的轻量级元数据条目。

    不包含完整的参数 schema（避免在上下文中占用过多空间），
    只保留名称和描述供搜索匹配使用。完整的工具对象在搜索匹配后返回。

    Attributes:
        name: 工具名称
        description: 工具描述
        tool: 完整的工具对象，仅在搜索匹配时返回
    """

    name: str
    description: str
    tool: BaseTool  # 完整工具对象，仅在搜索匹配时返回


class DeferredToolRegistry:
    """延迟工具注册表，支持正则搜索。

    管理所有延迟加载的工具，提供搜索和提升功能。

    搜索模式：
    1. select: 精确名称匹配（逗号分隔）
    2. +keyword: 名称包含 keyword，按剩余关键词排序
    3. 通用正则搜索：匹配名称和描述

    提升机制：
    promote() 从注册表中移除指定名称的工具，
    使其通过 DeferredToolFilterMiddleware 的过滤。
    """

    def __init__(self):
        self._entries: list[DeferredToolEntry] = []

    def register(self, tool: BaseTool) -> None:
        """注册一个延迟工具。

        从工具对象中提取名称和描述，创建轻量级条目。
        """
        self._entries.append(
            DeferredToolEntry(
                name=tool.name,
                description=tool.description or "",
                tool=tool,
            )
        )

    def promote(self, names: set[str]) -> None:
        """将工具从延迟注册表中移除（提升为活跃工具）。

        在 tool_search 返回工具 schema 后调用。
        被提升的工具将不再被 DeferredToolFilterMiddleware
        从 bind_tools 中过滤。

        Args:
            names: 要提升的工具名称集合
        """
        if not names:
            return
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.name not in names]
        promoted = before - len(self._entries)
        if promoted:
            logger.debug(f"Promoted {promoted} tool(s) from deferred to active: {names}")

    def search(self, query: str) -> list[BaseTool]:
        """按正则模式搜索延迟工具。

        支持三种查询形式（与 Claude Code 对齐）：
          - "select:name1,name2" — 精确名称匹配
          - "+keyword rest" — 名称必须包含 keyword，按 rest 排序
          - "keyword query" — 正则匹配名称和描述

        Args:
            query: 搜索查询字符串

        Returns:
            匹配的 BaseTool 对象列表（最多 MAX_RESULTS 个）
        """
        # 模式一：精确名称选择
        if query.startswith("select:"):
            names = {n.strip() for n in query[7:].split(",")}
            return [e.tool for e in self._entries if e.name in names][:MAX_RESULTS]

        # 模式二：名称包含关键词 + 排序
        if query.startswith("+"):
            parts = query[1:].split(None, 1)
            required = parts[0].lower()
            candidates = [e for e in self._entries if required in e.name.lower()]
            if len(parts) > 1:
                candidates.sort(
                    key=lambda e: _regex_score(parts[1], e),
                    reverse=True,
                )
            return [e.tool for e in candidates][:MAX_RESULTS]

        # 模式三：通用正则搜索
        try:
            regex = re.compile(query, re.IGNORECASE)
        except re.error:
            # 无效正则：转义后重新编译
            regex = re.compile(re.escape(query), re.IGNORECASE)

        scored = []
        for entry in self._entries:
            searchable = f"{entry.name} {entry.description}"
            if regex.search(searchable):
                # 名称匹配得分更高（2分），描述匹配得分较低（1分）
                score = 2 if regex.search(entry.name) else 1
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry.tool for _, entry in scored][:MAX_RESULTS]

    @property
    def entries(self) -> list[DeferredToolEntry]:
        """返回所有延迟工具条目的副本。"""
        return list(self._entries)

    @property
    def deferred_names(self) -> set[str]:
        """返回仍处于延迟状态的工具名称集合。"""
        return {entry.name for entry in self._entries}

    def contains(self, name: str) -> bool:
        """检查指定名称的工具是否仍处于延迟状态。"""
        return any(entry.name == name for entry in self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def _regex_score(pattern: str, entry: DeferredToolEntry) -> int:
    """计算正则模式在工具条目中的匹配得分（用于排序）。

    返回模式在名称和描述中匹配到的总次数。
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    return len(regex.findall(f"{entry.name} {entry.description}"))


# ── 每请求注册表（ContextVar） ──
#
# 使用 ContextVar 而非模块级全局变量，防止并发请求互相干扰。
# 在基于 asyncio 的 LangGraph 中，每个图运行在独立的异步上下文中执行，
# 因此每个请求获得独立的注册表值。对于通过 loop.run_in_executor 运行
# 的同步工具，Python 会将当前上下文复制到工作线程，
# 因此 ContextVar 值能正确继承。

_registry_var: contextvars.ContextVar[DeferredToolRegistry | None] = contextvars.ContextVar("deferred_tool_registry", default=None)


def get_deferred_registry() -> DeferredToolRegistry | None:
    """获取当前异步上下文的延迟工具注册表。"""
    return _registry_var.get()


def set_deferred_registry(registry: DeferredToolRegistry) -> None:
    """设置当前异步上下文的延迟工具注册表。"""
    _registry_var.set(registry)


def reset_deferred_registry() -> None:
    """重置当前异步上下文的延迟工具注册表。

    通常在新的请求/图运行开始时调用。
    """
    _registry_var.set(None)


# ── 工具 ──


@tool
def tool_search(query: str) -> str:
    """Fetches full schema definitions for deferred tools so they can be called.

    获取延迟工具的完整 schema 定义，使其可被调用。

    延迟工具以名称形式出现在系统提示的 <available-deferred-tools> 中。
    在获取之前，只知道名称——没有参数 schema，因此无法调用。
    此工具接受查询，在延迟工具列表中匹配，返回匹配工具的完整定义。
    一旦工具的 schema 出现在结果中，它就可以被调用了。

    查询形式：
      - "select:Read,Edit,Grep" — 按名称精确获取这些工具
      - "notebook jupyter" — 关键词搜索，返回最多 max_results 个最佳匹配
      - "+slack send" — 要求名称包含 "slack"，按剩余关键词排序

    Args:
        query: 查找延迟工具的查询。使用 "select:<工具名>" 进行
               精确选择，或使用关键词进行搜索。

    Returns:
        匹配的工具定义，格式为 JSON 数组。
    """
    registry = get_deferred_registry()
    if not registry:
        return "No deferred tools available."

    matched_tools = registry.search(query)
    if not matched_tools:
        return f"No tools found matching: {query}"

    # 使用 LangChain 的内置序列化生成 OpenAI function 格式。
    # 这是模型无关的：所有 LLM 都理解这种标准 schema。
    tool_defs = [convert_to_openai_function(t) for t in matched_tools[:MAX_RESULTS]]

    # 提升匹配的工具，使 DeferredToolFilterMiddleware 不再从
    # bind_tools 中过滤它们——LLM 现在有了完整的 schema 并可以调用。
    registry.promote({t.name for t in matched_tools[:MAX_RESULTS]})

    return json.dumps(tool_defs, indent=2, ensure_ascii=False)
