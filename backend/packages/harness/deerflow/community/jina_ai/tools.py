"""
Jina AI 网页抓取工具 — 基于 Jina Reader 的网页内容获取

本模块使用 Jina AI Reader API 提供网页内容抓取功能。通过 Jina Client
获取网页 HTML 内容，然后使用 Readability 提取器将其转换为干净的
Markdown 格式。

工具注册:
    - web_fetch_tool: 异步网页抓取工具，返回 Markdown 格式的页面内容

处理流程:
    1. 通过 Jina Client 抓取网页 HTML 内容
    2. 使用 Readability 提取器从 HTML 中提取正文内容
    3. 将提取的内容转换为 Markdown 格式
    4. 截断为 4096 字符后返回

配置方式:
    在 config.yaml 的 tools 段下配置:
        tools:
          web_fetch:
            timeout: 10

    环境变量:
        export JINA_API_KEY="your-jina-api-key"

设计决策:
    - 使用异步实现（async/await），适合与异步代理框架配合
    - HTML 到 Markdown 的转换通过 ReadabilityExtractor 在线程池中执行
      （避免阻塞事件循环）
    - Jina Client 返回错误时直接透传，不进行二次处理
"""

import asyncio

from langchain.tools import tool

from deerflow.community.jina_ai.jina_client import JinaClient
from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

# Readability 内容提取器实例（用于将 HTML 转换为 Markdown）
readability_extractor = ReadabilityExtractor()


@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """抓取给定 URL 的网页内容。
    仅抓取用户直接提供的 URL 或 web_search 和 web_fetch 工具返回的 URL。
    无法访问需要身份验证的内容（如私有 Google 文档或登录墙后的页面）。
    不要为没有 www. 的 URL 添加 www.。
    URL 必须包含协议：https://example.com 是有效的 URL，而 example.com 是无效的 URL。

    通过 Jina Reader API 获取网页 HTML，然后使用 Readability 提取器
    转换为干净的 Markdown 格式。

    Args:
        url: 要抓取内容的网页 URL。

    Returns:
        Markdown 格式的网页内容，截断为 4096 字符。
        如果 Jina API 返回错误，直接返回错误字符串。
    """
    jina_client = JinaClient()
    # 从配置中获取超时设置，默认为 10 秒
    timeout = 10
    config = get_app_config().get_tool_config("web_fetch")
    if config is not None and "timeout" in config.model_extra:
        timeout = config.model_extra.get("timeout")
    # 使用 Jina Client 获取 HTML 内容
    html_content = await jina_client.crawl(url, return_format="html", timeout=timeout)
    # 如果返回的是错误字符串，直接透传
    if isinstance(html_content, str) and html_content.startswith("Error:"):
        return html_content
    # 在线程池中执行 Readability 提取，避免阻塞异步事件循环
    article = await asyncio.to_thread(readability_extractor.extract_article, html_content)
    return article.to_markdown()[:4096]
