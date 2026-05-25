"""
Jina AI 客户端 — 基于 Jina Reader API 的网页内容抓取

本模块封装了 Jina AI Reader API 的 HTTP 客户端，用于将网页内容
转换为 HTML 或 Markdown 格式。Jina Reader 能够高效地抓取网页内容，
去除导航栏、广告等噪音，返回干净的页面内容。

API 概述:
    - 端点: https://r.jina.ai/
    - 方法: POST
    - 认证: 可选（有 API Key 可获得更高的请求速率限制）
    - 支持格式: HTML、Markdown、纯文本

使用方式:
    1. 免费使用：无需 API Key，有速率限制
    2. 付费使用：设置 JINA_API_KEY 环境变量获取更高限额

设计决策:
    - 使用 httpx 异步客户端，支持与异步工具函数配合
    - API Key 缺失时仅警告一次（全局标志 _api_key_warned）
    - 错误返回格式为 "Error: <信息>" 字符串，便于调用方判断
    - 支持自定义返回格式和超时设置

API Key 配置:
    通过环境变量设置:
        export JINA_API_KEY="your-jina-api-key"
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# API Key 缺失警告标志，确保只警告一次
_api_key_warned = False


class JinaClient:
    """Jina AI Reader API 客户端。

    封装了与 Jina Reader API 的 HTTP 交互，支持异步抓取网页内容。
    Jina Reader 能将网页内容转换为干净的 HTML 或 Markdown 格式。

    使用示例:
        client = JinaClient()
        html = await client.crawl("https://example.com", return_format="html")
    """

    async def crawl(self, url: str, return_format: str = "html", timeout: int = 10) -> str:
        """异步抓取指定 URL 的网页内容。

        向 Jina Reader API 发送 POST 请求，将网页内容转换为指定格式。
        如果设置了 JINA_API_KEY 环境变量，会在请求头中附带认证信息
        以获取更高的速率限制。

        Args:
            url: 要抓取的网页 URL。
            return_format: 返回内容的格式，可选值:
                - "html": HTML 格式（默认）
                - "markdown": Markdown 格式
                - "text": 纯文本格式
            timeout: 请求超时时间（秒），默认为 10。

        Returns:
            抓取到的网页内容字符串。如果抓取失败，返回
            "Error: <错误信息>" 格式的字符串。
        """
        global _api_key_warned
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
            "X-Timeout": str(timeout),
        }
        # 附加 API Key 用于认证（可选）
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        elif not _api_key_warned:
            _api_key_warned = True
            logger.warning("Jina API key is not set. Provide your own key to access a higher rate limit. See https://jina.ai/reader for more information.")
        data = {"url": url}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post("https://r.jina.ai/", headers=headers, json=data, timeout=timeout)

            # 检查 HTTP 状态码
            if response.status_code != 200:
                error_message = f"Jina API returned status {response.status_code}: {response.text}"
                logger.error(error_message)
                return f"Error: {error_message}"

            # 检查空响应
            if not response.text or not response.text.strip():
                error_message = "Jina API returned empty response"
                logger.error(error_message)
                return f"Error: {error_message}"

            return response.text
        except Exception as e:
            error_message = f"Request to Jina API failed: {type(e).__name__}: {e}"
            logger.warning(error_message)
            return f"Error: {error_message}"
