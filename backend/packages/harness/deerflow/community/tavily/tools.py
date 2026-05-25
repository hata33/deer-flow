"""
Tavily 搜索工具 — 基于 Tavily API 的网络搜索和网页抓取

本模块使用 Tavily 搜索引擎 API 提供网络搜索（web_search）和
网页内容抓取（web_fetch）两种工具。Tavily 是一个专为 AI 代理设计的
搜索 API，能够返回结构化的搜索结果和网页内容。

提供的工具:
    - web_search_tool: 网络搜索工具，返回与查询相关的网页结果列表
    - web_fetch_tool: 网页抓取工具，获取指定 URL 的网页内容

配置方式:
    在 config.yaml 的 tools 段下配置 web_search 工具:
        tools:
          web_search:
            api_key: "your-tavily-api-key"
            max_results: 5

设计决策:
    - 使用 LangChain 的 @tool 装饰器注册为可调用工具
    - 搜索结果标准化为统一的 {title, url, snippet} 格式
    - 网页抓取结果截断为 4096 字符，平衡信息完整性和上下文长度
    - API Key 通过应用配置管理，支持统一的安全策略
"""

import json

from langchain.tools import tool
from tavily import TavilyClient

from deerflow.config import get_app_config


def _get_tavily_client() -> TavilyClient:
    """创建并返回配置好的 Tavily API 客户端。

    从应用配置中获取 Tavily API Key。如果未配置 API Key，
    TavilyClient 将使用环境变量 TAVILY_API_KEY。

    Returns:
        配置好的 TavilyClient 实例。
    """
    config = get_app_config().get_tool_config("web_search")
    api_key = None
    if config is not None and "api_key" in config.model_extra:
        api_key = config.model_extra.get("api_key")
    return TavilyClient(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """搜索网络。

    使用 Tavily 搜索引擎执行网络搜索，返回标准化的搜索结果列表。
    结果数量可通过配置文件中的 max_results 参数调整。

    Args:
        query: 搜索查询关键词。

    Returns:
        JSON 格式的搜索结果列表，每条结果包含:
        - title: 网页标题
        - url: 网页链接
        - snippet: 内容摘要
    """
    # 从配置中获取最大结果数，默认为 5 条
    config = get_app_config().get_tool_config("web_search")
    max_results = 5
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results")

    client = _get_tavily_client()
    res = client.search(query, max_results=max_results)
    # 将搜索结果标准化为统一格式
    normalized_results = [
        {
            "title": result["title"],
            "url": result["url"],
            "snippet": result["content"],
        }
        for result in res["results"]
    ]
    json_results = json.dumps(normalized_results, indent=2, ensure_ascii=False)
    return json_results


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """抓取给定 URL 的网页内容。
    仅抓取用户直接提供的 URL 或 web_search 和 web_fetch 工具返回的 URL。
    无法访问需要身份验证的内容（如私有 Google 文档或登录墙后的页面）。
    不要为没有 www. 的 URL 添加 www.。
    URL 必须包含协议：https://example.com 是有效的 URL，而 example.com 是无效的 URL。

    Args:
        url: 要抓取内容的网页 URL。

    Returns:
        网页内容（Markdown 格式，带标题），截断为 4096 字符。
        如果抓取失败，返回错误信息。
    """
    client = _get_tavily_client()
    res = client.extract([url])
    # 处理抓取失败的情况
    if "failed_results" in res and len(res["failed_results"]) > 0:
        return f"Error: {res['failed_results'][0]['error']}"
    elif "results" in res and len(res["results"]) > 0:
        result = res["results"][0]
        # 将内容截断为 4096 字符以控制上下文长度
        return f"# {result['title']}\n\n{result['raw_content'][:4096]}"
    else:
        return "Error: No results found"
