"""
DuckDuckGo 网络搜索工具 — 免费的网络搜索实现

本模块使用 DuckDuckGo 搜索引擎提供网络搜索功能。
DuckDuckGo 搜索不需要 API Key，适合快速部署和测试场景。

特性:
    - 无需 API Key（免费使用）
    - 支持区域设置（默认全球搜索 "wt-wt"）
    - 支持安全搜索级别控制
    - 搜索结果标准化为统一格式

依赖:
    - ddgs: DuckDuckGo 搜索的 Python 客户端库

配置方式:
    在 config.yaml 的 tools 段下配置:
        tools:
          web_search:
            max_results: 5

设计决策:
    - 搜索结果标准化为 {title, url, content} 格式，与其他搜索工具保持一致
    - 错误处理使用 JSON 格式返回，包含 query 字段便于调试
    - 使用 ddgs 库的超时参数（30 秒）防止长时间阻塞
"""

import json
import logging

from langchain.tools import tool

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)


def _search_text(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    safesearch: str = "moderate",
) -> list[dict]:
    """使用 DuckDuckGo 执行文本搜索。

    通过 ddgs 库调用 DuckDuckGo 搜索 API，获取与查询关键词匹配的
    网页搜索结果。

    Args:
        query: 搜索关键词字符串。
        max_results: 最大返回结果数量，默认为 5。
        region: 搜索区域设置（例如 "wt-wt" 表示全球，
                "cn-zh" 表示中国），默认为 "wt-wt"。
        safesearch: 安全搜索级别，可选值:
            - "on": 严格过滤
            - "moderate": 适度过滤（默认）
            - "off": 不过滤

    Returns:
        搜索结果字典列表。每个字典包含 title、href/link、body/snippet 等字段。
        如果搜索失败或未安装 ddgs 库，返回空列表。
    """
    try:
        from ddgs import DDGS
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []

    ddgs = DDGS(timeout=30)

    try:
        results = ddgs.text(
            query,
            region=region,
            safesearch=safesearch,
            max_results=max_results,
        )
        return list(results) if results else []

    except Exception as e:
        logger.error(f"Failed to search web: {e}")
        return []


@tool("web_search", parse_docstring=True)
def web_search_tool(
    query: str,
    max_results: int = 5,
) -> str:
    """搜索网络信息。使用此工具从互联网查找最新信息、新闻、文章和事实。

    通过 DuckDuckGo 搜索引擎执行网络搜索，返回与查询关键词匹配的
    网页结果。搜索结果包含标题、链接和内容摘要。

    Args:
        query: 描述搜索内容的查询关键词。更具体的关键词能获得更好的结果。
        max_results: 返回结果的最大数量。默认为 5。

    Returns:
        JSON 格式的搜索结果，包含:
        - query: 原始搜索查询
        - total_results: 结果总数
        - results: 结果列表，每条包含 title、url 和 content
    """
    config = get_app_config().get_tool_config("web_search")

    # 如果配置中设置了 max_results，则覆盖默认值
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results", max_results)

    results = _search_text(
        query=query,
        max_results=max_results,
    )

    if not results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    # 将搜索结果标准化为统一的 {title, url, content} 格式
    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "content": r.get("body", r.get("snippet", "")),
        }
        for r in results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)
