"""
Firecrawl 网络搜索工具 — 基于 Firecrawl API 的搜索和网页抓取

本模块使用 Firecrawl API 提供网络搜索和网页内容抓取功能。
Firecrawl 是一个专为 AI 应用设计的网页抓取服务，能够将网页
内容转换为干净的 Markdown 格式。

提供的工具:
    - web_search_tool: 网络搜索工具，返回与查询相关的网页结果列表
    - web_fetch_tool: 网页抓取工具，获取指定 URL 的 Markdown 内容

特性:
    - 搜索结果包含标题、URL 和描述
    - 网页抓取直接返回 Markdown 格式内容
    - 支持配置最大搜索结果数
    - 所有异常均被捕获并返回友好的错误消息

配置方式:
    在 config.yaml 的 tools 段下配置:
        tools:
          web_search:
            api_key: "your-firecrawl-api-key"
            max_results: 5
          web_fetch:
            api_key: "your-firecrawl-api-key"

设计决策:
    - 使用 LangChain 的 @tool 装饰器注册为可调用工具
    - 搜索结果通过 getattr 安全访问属性（SearchResultWeb 对象）
    - 网页抓取使用 scrape API 的 markdown 格式输出
    - 内容截断为 4096 字符，平衡信息完整性和上下文长度
"""

import json

from firecrawl import FirecrawlApp
from langchain.tools import tool

from deerflow.config import get_app_config


def _get_firecrawl_client(tool_name: str = "web_search") -> FirecrawlApp:
    """创建并返回配置好的 Firecrawl API 客户端。

    从应用配置中获取 Firecrawl API Key。根据不同的工具名称
    （web_search 或 web_fetch）从对应的配置段读取 API Key。

    Args:
        tool_name: 工具名称，用于确定从哪个配置段读取 API Key。
                   默认为 "web_search"。

    Returns:
        配置好的 FirecrawlApp 实例。
    """
    config = get_app_config().get_tool_config(tool_name)
    api_key = None
    if config is not None and "api_key" in config.model_extra:
        api_key = config.model_extra.get("api_key")
    return FirecrawlApp(api_key=api_key)  # type: ignore[arg-type]


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """搜索网络。

    使用 Firecrawl 搜索 API 执行网络搜索，返回包含标题、URL 和
    描述的搜索结果列表。

    Args:
        query: 搜索查询字符串。

    Returns:
        JSON 格式的搜索结果列表，每条结果包含:
        - title: 网页标题
        - url: 网页链接
        - snippet: 内容描述
        如果搜索失败，返回 "Error: <错误信息>" 格式的字符串。
    """
    try:
        config = get_app_config().get_tool_config("web_search")
        max_results = 5
        if config is not None:
            max_results = config.model_extra.get("max_results", max_results)

        client = _get_firecrawl_client("web_search")
        result = client.search(query, limit=max_results)

        # result.web 包含 SearchResultWeb 对象列表
        # 使用 getattr 安全访问属性，避免 AttributeError
        web_results = result.web or []
        normalized_results = [
            {
                "title": getattr(item, "title", "") or "",
                "url": getattr(item, "url", "") or "",
                "snippet": getattr(item, "description", "") or "",
            }
            for item in web_results
        ]
        json_results = json.dumps(normalized_results, indent=2, ensure_ascii=False)
        return json_results
    except Exception as e:
        return f"Error: {str(e)}"


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """抓取给定 URL 的网页内容。
    仅抓取用户直接提供的 URL 或 web_search 和 web_fetch 工具返回的 URL。
    无法访问需要身份验证的内容（如私有 Google 文档或登录墙后的页面）。
    不要为没有 www. 的 URL 添加 www.。
    URL 必须包含协议：https://example.com 是有效的 URL，而 example.com 是无效的 URL。

    使用 Firecrawl 的 scrape API 将网页转换为 Markdown 格式。

    Args:
        url: 要抓取内容的网页 URL。

    Returns:
        Markdown 格式的网页内容（带标题），截断为 4096 字符。
        如果抓取失败或无内容，返回错误信息。
    """
    try:
        client = _get_firecrawl_client("web_fetch")
        result = client.scrape(url, formats=["markdown"])

        markdown_content = result.markdown or ""
        metadata = result.metadata
        title = metadata.title if metadata and metadata.title else "Untitled"

        if not markdown_content:
            return "Error: No content found"
    except Exception as e:
        return f"Error: {str(e)}"

    return f"# {title}\n\n{markdown_content[:4096]}"
