"""文件转换工具。

使用 markitdown 将文档文件（PDF、PPT、Excel、Word）转换为 Markdown 格式。
纯工具函数——无 FastAPI 或 HTTP 依赖。
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 支持转换为 Markdown 的文件扩展名
CONVERTIBLE_EXTENSIONS = {
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".doc",
    ".docx",
}


async def convert_file_to_markdown(file_path: Path) -> Path | None:
    """使用 markitdown 将文档文件转换为 Markdown。

    转换后的 .md 文件保存在原文件同目录下，文件名相同但扩展名为 .md。

    Args:
        file_path: 待转换的文件路径。

    Returns:
        转换后的 Markdown 文件路径，失败时返回 None。
    """
    try:
        from markitdown import MarkItDown

        md = MarkItDown()
        result = md.convert(str(file_path))

        md_path = file_path.with_suffix(".md")
        md_path.write_text(result.text_content, encoding="utf-8")

        logger.info(f"Converted {file_path.name} to markdown: {md_path.name}")
        return md_path
    except Exception as e:
        logger.error(f"Failed to convert {file_path.name} to markdown: {e}")
        return None
