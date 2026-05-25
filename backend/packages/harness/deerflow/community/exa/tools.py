"""
Exa 搜索工具 — 基于 Exa AI 的网络搜索和网页抓取

本模块使用 Exa AI 搜索引擎 API 提供网络搜索和网页内容抓取功能。
Exa 是一个专为 AI 应用设计的搜索引擎，支持语义搜索和高亮内容提取。

提供的工具:
    - web_search_tool: 网络搜索工具，支持自动/关键词/神经搜索模式
    - web_fetch_tool: 网页抓取工具，获取指定 URL 的页面文本内容

特性:
    - 支持多种搜索类型（auto/keyword/neural）
    - 可配置的内容高亮长度
    - 搜索结果包含高亮摘要（highlights）
    - 网页抓取直接获取页面文本内容

配置方式:
    在 config.yaml 的 tools 段下配置:
        tools:
          web_search:
            api_key: "your-exa-api-key"
            max_results: 5
            search_type: "auto"
            contents_max_characters: 1000
          web_fetch:
            api_key: "your-exa-api-key"

设计决策:
    - 搜索结果标准化为 {title, url, snippet} 格式
    - snippet 使用换行符连接多个高亮片段
    - 网页抓取结果截断为 4096 字符
    - 所有异常均被捕获并返回友好的错误消息，确保代理不会因搜索失败而中断
"""

import json

from exa_py import Exa
from langchain.tools import tool

from deerflow.config import get_app_config


def _get_exa_client(tool_name: str = "web_search") -> Exa:
    """创建并返回配置好的 Exa API 客户端。

    从应用配置中获取 Exa API Key。根据不同的工具名称
    （web_search 或 web_fetch）从对应的配置段读取 API Key。

    Args:
        tool_name: 工具名称，用于确定从哪个配置段读取 API Key。
                   默认为 "web_search"。

    Returns:
        配置好的 Exa 客户端实例。
    """
    config = get_app_config().get_tool_config(tool_name)
    api_key = None
    if config is not None and "api_key" in config.model_extra:
        api_key = config.model_extra.get("api_key")
    return Exa(api_key=api_key)


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """搜索网络。

    使用 Exa AI 搜索引擎执行网络搜索。支持自动、关键词和神经搜索模式，
    返回带高亮摘要的搜索结果。

    Args:
        query: 搜索查询字符串。

    Returns:
        JSON 格式的搜索结果列表，每条结果包含:
        - title: 网页标题
        - url: 网页链接
        - snippet: 高亮内容摘要（多个高亮片段以换行分隔）
        如果搜索失败，返回 "Error: <错误信息>" 格式的字符串。
    """
    try:
        config = get_app_config().get_tool_config("web_search")
        # 默认参数配置
        max_results = 5
        search_type = "auto"
        contents_max_characters = 1000
        if config is not None:
            max_results = config.model_extra.get("max_results", max_results)
            search_type = config.model_extra.get("search_type", search_type)
            contents_max_characters = config.model_extra.get("contents_max_characters", contents_max_characters)

        client = _get_exa_client()
        res = client.search(
            query,
            type=search_type,
            num_results=max_results,
            contents={"highlights": {"max_characters": contents_max_characters}},
        )

        # 将搜索结果标准化为统一格式，高亮片段以换行连接
        normalized_results = [
            {
                "title": result.title or "",
                "url": result.url or "",
                "snippet": "\n".join(result.highlights) if result.highlights else "",
            }
            for result in res.results
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

    使用 Exa AI 的内容获取 API 提取网页文本内容。

    Args:
        url: 要抓取内容的网页 URL。

    Returns:
        Markdown 格式的网页内容（带标题），截断为 4096 字符。
        如果抓取失败，返回 "Error: <错误信息>" 格式的字符串。
    """
    try:
        client = _get_exa_client("web_fetch")
        res = client.get_contents([url], text={"max_characters": 4096})

        if res.results:
            result = res.results[0]
            title = result.title or "Untitled"
            text = result.text or ""
            return f"# {title}\n\n{text[:4096]}"
        else:
            return "Error: No results found"
    except Exception as e:
        return f"Error: {str(e)}"
