"""网页内容提取与 Markdown 转换工具。

从 HTML 页面中提取正文内容，转换为 Markdown 格式。
支持将 Markdown 中的图片引用转为多模态消息格式。
"""

import logging
import re
import subprocess
from urllib.parse import urljoin

from markdownify import markdownify as md
from readabilipy import simple_json_from_html_string

logger = logging.getLogger(__name__)


class Article:
    """从网页提取的文章内容。

    Attributes:
        title: 文章标题。
        html_content: 文章正文的 HTML 内容。
    """

    url: str

    def __init__(self, title: str, html_content: str):
        self.title = title
        self.html_content = html_content

    def to_markdown(self, including_title: bool = True) -> str:
        """将文章内容转换为 Markdown 格式。

        Args:
            including_title: 是否包含标题行。

        Returns:
            Markdown 格式的文章内容。
        """
        markdown = ""
        if including_title:
            markdown += f"# {self.title}\n\n"

        if self.html_content is None or not str(self.html_content).strip():
            markdown += "*No content available*\n"
        else:
            markdown += md(self.html_content)

        return markdown

    def to_message(self) -> list[dict]:
        """将文章转换为多模态消息格式（文本 + 图片 URL）。

        解析 Markdown 中的图片引用，转为交替的文本和图片 URL 块，
        图片 URL 通过 urljoin 处理相对路径。

        Returns:
            多模态消息块列表。
        """
        image_pattern = r"!\[.*?\]\((.*?)\)"

        content: list[dict[str, str]] = []
        markdown = self.to_markdown()

        if not markdown or not markdown.strip():
            return [{"type": "text", "text": "No content available"}]

        parts = re.split(image_pattern, markdown)

        for i, part in enumerate(parts):
            if i % 2 == 1:
                # 奇数位是图片 URL（正则捕获组）
                image_url = urljoin(self.url, part.strip())
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            else:
                # 偶数位是文本内容
                text_part = part.strip()
                if text_part:
                    content.append({"type": "text", "text": text_part})

        if not content:
            content = [{"type": "text", "text": "No content available"}]

        return content


class ReadabilityExtractor:
    """基于 Readability.js 的网页正文提取器。

    优先使用 Readability.js（Node.js）提取，失败时降级到纯 Python 提取。
    """

    def extract_article(self, html: str) -> Article:
        """从 HTML 中提取文章正文。

        Args:
            html: 原始 HTML 字符串。

        Returns:
            提取的 Article 实例。
        """
        try:
            article = simple_json_from_html_string(html, use_readability=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            # Readability.js 不可用时降级到纯 Python 提取
            stderr = getattr(exc, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr_info = f"; stderr={stderr.strip()}" if isinstance(stderr, str) and stderr.strip() else ""
            logger.warning(
                "Readability.js extraction failed with %s%s; falling back to pure-Python extraction",
                type(exc).__name__,
                stderr_info,
                exc_info=True,
            )
            article = simple_json_from_html_string(html, use_readability=False)

        html_content = article.get("content")
        if not html_content or not str(html_content).strip():
            html_content = "No content could be extracted from this page"

        title = article.get("title")
        if not title or not str(title).strip():
            title = "Untitled"

        return Article(title=title, html_content=html_content)
