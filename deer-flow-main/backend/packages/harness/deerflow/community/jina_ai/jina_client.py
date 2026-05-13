"""Jina AI Reader 客户端。

通过 Jina Reader API 抓取网页内容，支持 HTML 返回格式和超时配置。
免费版有速率限制，配置 JINA_API_KEY 可获取更高配额。
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


class JinaClient:
    """Jina Reader API 客户端，用于抓取网页内容。"""

    def crawl(self, url: str, return_format: str = "html", timeout: int = 10) -> str:
        """抓取指定 URL 的内容。

        Args:
            url: 目标 URL。
            return_format: 返回格式（html/markdown 等）。
            timeout: 请求超时秒数。
        """
        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
            "X-Timeout": str(timeout),
        }
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        else:
            logger.warning("Jina API key is not set. Provide your own key to access a higher rate limit. See https://jina.ai/reader for more information.")
        data = {"url": url}
        try:
            response = requests.post("https://r.jina.ai/", headers=headers, json=data)

            if response.status_code != 200:
                error_message = f"Jina API returned status {response.status_code}: {response.text}"
                logger.error(error_message)
                return f"Error: {error_message}"

            if not response.text or not response.text.strip():
                error_message = "Jina API returned empty response"
                logger.error(error_message)
                return f"Error: {error_message}"

            return response.text
        except Exception as e:
            error_message = f"Request to Jina API failed: {str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"
