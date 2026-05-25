"""网页内容提取模块 —— HTML 正文提取与 Markdown 转换。

本模块为 DeerFlow 系统提供网页内容提取能力，将 HTML 页面中的正文内容
（去除导航栏、广告、侧边栏等噪音）提取为结构化的 ``Article`` 对象，
并支持转换为 Markdown 格式或 LLM 多模态消息格式。

核心组件：
    - :class:`Article` —— 文章数据模型，封装标题和 HTML 正文，
      提供 ``to_markdown()`` 和 ``to_message()`` 两种输出格式。
    - :class:`ReadabilityExtractor` —— 内容提取器，封装 Readability.js
      算法，支持自动降级到纯 Python 提取。

提取策略：
    本模块使用 ``readabilipy`` 库作为提取引擎，该库提供了两种模式：
    1. **Readability.js 模式**（默认）—— 调用 Mozilla 的 Readability.js
       算法（通过 Node.js 子进程），提取精度高，但需要系统安装 Node.js。
    2. **纯 Python 模式**（降级） —— 当 Node.js 不可用时，
       自动降级到 ``readabilipy`` 的纯 Python 实现，精度略低但无外部依赖。

应用场景：
    - DeerFlow Agent 在浏览网页时，使用本模块提取正文内容。
    - 提取后的 Markdown 可作为 LLM 的上下文输入。
    - ``to_message()`` 生成的多模态消息可包含页面中的图片。

依赖说明：
    - ``readabilipy`` —— Readability 算法的 Python 封装
    - ``markdownify`` —— HTML → Markdown 转换库
    - （可选）Node.js —— 用于高精度的 Readability.js 模式
"""

import logging
import re
import subprocess
from urllib.parse import urljoin

from markdownify import markdownify as md
from readabilipy import simple_json_from_html_string

logger = logging.getLogger(__name__)


class Article:
    """文章数据模型 —— 封装从网页中提取的正文内容。

    本类作为网页内容提取的标准输出格式，存储文章标题和 HTML 正文，
    并提供多种输出转换方法。设计为简单的数据容器，不包含提取逻辑。

    Attributes:
        title (str): 文章标题。
        html_content (str): 文章正文的 HTML 内容。
        url (str): 文章来源 URL（用于解析相对图片路径）。
    """

    url: str

    def __init__(self, title: str, html_content: str):
        """初始化文章实例。

        Args:
            title: 文章标题。如果提取失败，调用者应传入 ``"Untitled"`` 等默认值。
            html_content: 文章正文的 HTML 片段（不含 <html>/<body> 等外层标签）。
        """
        self.title = title
        self.html_content = html_content

    def to_markdown(self, including_title: bool = True) -> str:
        """将文章内容转换为 Markdown 格式。

        使用 ``markdownify`` 库将 HTML 正文转换为 Markdown 文本。
        可选择在输出开头添加一级标题行。

        Args:
            including_title: 是否在 Markdown 输出中包含标题行（默认 ``True``）。
                设为 ``False`` 时仅输出正文内容。

        Returns:
            Markdown 格式的字符串。如果正文为空，返回占位文本
            ``"*No content available*"``。
        """
        markdown = ""
        if including_title:
            markdown += f"# {self.title}\n\n"

        # 处理正文为空或纯空白的情况，返回有意义的占位文本
        if self.html_content is None or not str(self.html_content).strip():
            markdown += "*No content available*\n"
        else:
            # markdownify 将 HTML 标签转换为 Markdown 语法
            markdown += md(self.html_content)

        return markdown

    def to_message(self) -> list[dict]:
        """将文章转换为 LLM 多模态消息格式。

        解析 Markdown 内容中的图片引用（``![alt](url)``），将其拆分为
        交替排列的文本块和图片块，形成 LangChain/OpenAI 多模态消息格式。
        图片 URL 使用 ``urljoin`` 解析为绝对路径，确保相对路径的图片
        也能正确加载。

        返回格式为 ``content`` 字段的值，可直接用于 LangChain 的
        ``HumanMessage(content=...)`` 或 OpenAI API 的消息体。

        Args:
            无额外参数，使用实例的 ``title`` 和 ``html_content``。

        Returns:
            消息块列表，每个元素为以下格式之一：
            - 文本块：``{"type": "text", "text": "..."}``
            - 图片块：``{"type": "image_url", "image_url": {"url": "..."}}``
            如果内容为空，返回 ``[{"type": "text", "text": "No content available"}]``。

        Note:
            图片 URL 通过 ``urljoin(self.url, ...)`` 转换为绝对路径，
            确保Markdown中引用的相对路径图片能被 LLM 正确访问。
        """
        # 匹配 Markdown 图片语法：![任意替代文本](图片URL)
        image_pattern = r"!\[.*?\]\((.*?)\)"

        content: list[dict[str, str]] = []
        markdown = self.to_markdown()

        # 空内容快速返回占位消息
        if not markdown or not markdown.strip():
            return [{"type": "text", "text": "No content available"}]

        # 使用正则分割 Markdown 文本，将图片引用从文本中分离出来。
        # re.split 在捕获组存在时，会将匹配到的组内容（图片 URL）
        # 作为独立元素插入结果列表，形成 [文本, URL, 文本, URL, ...] 的交替模式
        parts = re.split(image_pattern, markdown)

        for i, part in enumerate(parts):
            if i % 2 == 1:
                # 奇数索引位置是捕获组内容（图片 URL）
                # 使用 urljoin 将相对路径转换为绝对路径，
                # 以 self.url（文章来源页面的 URL）作为基准
                image_url = urljoin(self.url, part.strip())
                content.append({"type": "image_url", "image_url": {"url": image_url}})
            else:
                # 偶数索引位置是普通文本
                text_part = part.strip()
                if text_part:
                    content.append({"type": "text", "text": text_part})

        # 兜底处理：如果分割后所有部分都为空（理论上不太可能），
        # 返回占位消息避免返回空列表
        if not content:
            content = [{"type": "text", "text": "No content available"}]

        return content


class ReadabilityExtractor:
    """网页正文提取器 —— 封装 Readability.js 算法。

    本类使用 ``readabilipy`` 库从 HTML 页面中提取正文内容，自动去除
    导航栏、广告、侧边栏等非正文元素。提取失败时自动降级到纯 Python 模式。

    提取流程：
    1. 首先尝试使用 Readability.js 模式（需要 Node.js）。
    2. 如果 Node.js 不可用或提取失败，自动降级到纯 Python 模式。
    3. 对提取结果进行空值处理，确保返回的 ``Article`` 始终有效。
    """

    def extract_article(self, html: str) -> Article:
        """从 HTML 字符串中提取文章正文。

        使用 Readability 算法从完整 HTML 页面中识别并提取正文区域，
        去除导航、广告、页脚等噪音内容。提取过程具有自动降级机制：
        当 Readability.js（Node.js 子进程）不可用时，自动切换到
        纯 Python 实现继续提取。

        Args:
            html: 完整 HTML 页面字符串，包含 ``<html>``、``<body>`` 等标签。

        Returns:
            :class:`Article` 实例，包含：
            - ``title``: 文章标题（提取失败时为 ``"Untitled"``）
            - ``html_content``: 正文 HTML 片段（提取失败时为占位文本）

        Note:
            当 Readability.js 提取失败时，会记录 ``WARNING`` 级别日志，
            包含异常类型和 stderr 输出（如有），便于排查 Node.js 环境问题。
        """
        try:
            # 优先使用 Readability.js 模式（精度更高），
            # 内部会通过 Node.js 子进程执行 Mozilla 的 Readability 算法
            article = simple_json_from_html_string(html, use_readability=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            # CalledProcessError: Node.js 子进程执行失败
            # FileNotFoundError: 系统未安装 Node.js
            # 两种情况都说明 Readability.js 模式不可用，需要降级
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
            # 降级到纯 Python 模式，无需 Node.js，精度略低但保证可用
            article = simple_json_from_html_string(html, use_readability=False)

        # 提取正文 HTML，处理空值情况
        html_content = article.get("content")
        if not html_content or not str(html_content).strip():
            html_content = "No content could be extracted from this page"

        # 提取标题，处理空值情况
        title = article.get("title")
        if not title or not str(title).strip():
            title = "Untitled"

        return Article(title=title, html_content=html_content)
