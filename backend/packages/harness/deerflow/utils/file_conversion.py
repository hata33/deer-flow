"""文档格式转换模块 —— 多格式文档转 Markdown 工具。

本模块实现了 DeerFlow 系统中文件上传后的文档格式转换功能，将 PDF、
PowerPoint、Excel、Word 等格式的文档统一转换为 Markdown 文本，
供 LLM 作为上下文使用。

支持的输入格式：
    - PDF（``.pdf``）—— 双引擎策略（见下文）
    - PowerPoint（``.ppt``, ``.pptx``）—— 通过 MarkItDown
    - Excel（``.xls``, ``.xlsx``）—— 通过 MarkItDown
    - Word（``.doc``, ``.docx``）—— 通过 MarkItDown

PDF 转换策略（auto 模式）：
    PDF 是最复杂的转换场景，本模块实现了自适应双引擎策略：

    1. **首选 pymupdf4llm** —— 如果已安装，优先使用 pymupdf4llm。
       它对文本型 PDF 有更好的标题检测能力，转换速度通常更快。
    2. **输出稀疏检测** —— 如果 pymupdf4llm 的输出过短
       （每页 < 50 字符，或总长 < 200 字符但无法获取页数时），
       判定为图片型 PDF 或加密 PDF，自动降级到 MarkItDown。
    3. **兜底 MarkItDown** —— 如果 pymupdf4llm 未安装或上述降级触发，
       使用 MarkItDown 进行转换（内置 OCR 支持，适合图片型 PDF）。

    可通过配置项 ``pdf_converter`` 强制指定引擎：
    - ``"auto"``（默认）—— 自适应双引擎
    - ``"pymupdf4llm"`` —— 仅使用 pymupdf4llm，不降级
    - ``"markitdown"`` —— 直接使用 MarkItDown

大文件处理：
    文件大小超过 ``_ASYNC_THRESHOLD_BYTES``（1 MB）时，转换操作
    会在后台线程池中通过 ``asyncio.to_thread()`` 执行，避免阻塞
    事件循环。小文件在当前线程同步执行（线程调度开销 > 转换时间）。

文档大纲提取：
    ``extract_outline`` 函数从转换后的 Markdown 文件中提取标题层级，
    支持 pymupdf4llm 生成的三种标题样式：
    1. 标准 Markdown 标题（``# `` / ``## `` 等）
    2. 加粗结构性标题（``**ITEM 1. BUSINESS**``，常见于 SEC 文件）
    3. 分离式加粗标题（``**1** **Introduction**``，常见于学术论文）

性能考虑：
    - 大纲条目上限为 ``MAX_OUTLINE_ENTRIES``（50），防止超长文档
      注入过多上下文导致 token 消耗过高。
    - 正则表达式经过线性复杂度设计，避免 ReDoS 攻击。

无外部框架依赖：
    本模块为纯工具函数层，**不依赖 FastAPI 或任何 HTTP 框架**，
    可在任意 Python 环境中使用。
"""

import asyncio
import logging
import re
from pathlib import Path

from deerflow.config.app_config import get_app_config

logger = logging.getLogger(__name__)

# 需要转换为 Markdown 的文件扩展名集合。
# 上传这些格式的文件时，系统会自动触发转换流程。
CONVERTIBLE_EXTENSIONS = {
    ".pdf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".doc",
    ".docx",
}

# 异步转换的字节大小阈值。
# 超过此大小的文件会在后台线程中转换，避免阻塞事件循环。
# 小文件（< 1 MB）通常在 1 秒内完成，同步执行的调度开销更低。
_ASYNC_THRESHOLD_BYTES = 1 * 1024 * 1024  # 1 MB

# pymupdf4llm 输出的每页最少字符数阈值。
# 低于此值说明 PDF 可能是图片型的（OCR 文本接近 0）或加密的。
# 正常文本型 PDF 每页产出 200-2000 字符，50 字符/页提供了充分的安全余量。
# 当无法获取页数时，回退到绝对阈值 200 字符。
_MIN_CHARS_PER_PAGE = 50


def _pymupdf_output_too_sparse(text: str, file_path: Path) -> bool:
    """检测 pymupdf4llm 输出是否过于稀疏（疑似图片型或加密 PDF）。

    使用"每页字符数"而非"绝对字符数"作为判断标准，使短文档（页数少、
    字符少）和长文档（页数多、字符多）都能被正确判断。

    判断逻辑：
    - 如果能获取页数：``chars / pages < 50`` 视为稀疏。
    - 如果无法获取页数：``chars < 200`` 视为稀疏。

    Args:
        text: pymupdf4llm 输出的 Markdown 文本。
        file_path: 原始 PDF 文件路径（用于获取页数）。

    Returns:
        ``True`` 如果输出疑似图片型/加密 PDF，应降级到 MarkItDown。
    """
    chars = len(text.strip())
    doc = None
    pages: int | None = None
    try:
        import pymupdf

        doc = pymupdf.open(str(file_path))
        pages = len(doc)
    except Exception:
        # 打开文件失败时 pages 保持 None，后续使用绝对阈值判断
        pass
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    if pages is not None and pages > 0:
        return (chars / pages) < _MIN_CHARS_PER_PAGE
    # 无法获取页数时的兜底策略：使用绝对字符数阈值
    return chars < 200


def _convert_pdf_with_pymupdf4llm(file_path: Path) -> str | None:
    """尝试使用 pymupdf4llm 转换 PDF。

    pymupdf4llm 是基于 PyMuPDF 的 PDF 转 Markdown 库，对文本型 PDF
    有更好的标题检测和结构保持能力。如果未安装或转换失败（如加密/损坏的
    PDF），返回 ``None``，由调用者决定降级策略。

    Args:
        file_path: PDF 文件路径。

    Returns:
        转换后的 Markdown 文本，或 ``None``（未安装或转换失败时）。
    """
    try:
        import pymupdf4llm
    except ImportError:
        # pymupdf4llm 未安装，返回 None 让调用者降级到 MarkItDown
        return None

    try:
        return pymupdf4llm.to_markdown(str(file_path))
    except Exception:
        # 转换失败（加密、损坏等），记录异常后返回 None
        logger.exception("pymupdf4llm failed to convert %s; falling back to MarkItDown", file_path.name)
        return None


def _convert_with_markitdown(file_path: Path) -> str:
    """使用 MarkItDown 转换任意支持的文件格式为 Markdown。

    MarkItDown 是微软开源的文档转换工具，支持 PDF（含 OCR）、
    PowerPoint、Excel、Word 等格式。作为通用兜底转换引擎。

    Args:
        file_path: 待转换的文件路径。

    Returns:
        转换后的 Markdown 文本。
    """
    from markitdown import MarkItDown

    md = MarkItDown()
    return md.convert(str(file_path)).text_content


def _do_convert(file_path: Path, pdf_converter: str) -> str:
    """同步执行文档格式转换（直接调用或通过 ``asyncio.to_thread`` 调用）。

    根据文件类型和配置选择合适的转换引擎：
    - 非 PDF 文件：始终使用 MarkItDown。
    - PDF 文件 + ``"markitdown"`` 配置：直接使用 MarkItDown。
    - PDF 文件 + ``"auto"`` 配置：先尝试 pymupdf4llm，按需降级。
    - PDF 文件 + ``"pymupdf4llm"`` 配置：仅使用 pymupdf4llm，不降级。

    Args:
        file_path: 待转换的文件路径。
        pdf_converter: PDF 转换引擎选择，取值为
            ``"auto"`` | ``"pymupdf4llm"`` | ``"markitdown"``。

    Returns:
        转换后的 Markdown 文本。
    """
    is_pdf = file_path.suffix.lower() == ".pdf"

    if is_pdf and pdf_converter != "markitdown":
        # 尝试使用 pymupdf4llm（auto 或显式指定）
        pymupdf_text = _convert_pdf_with_pymupdf4llm(file_path)

        if pymupdf_text is not None:
            # pymupdf4llm 已安装且转换成功
            if pdf_converter == "pymupdf4llm":
                # 显式指定模式：直接使用输出，不管长度如何
                return pymupdf_text
            # auto 模式：检查输出是否过于稀疏。
            # 使用"每页字符数"区分图片型 PDF（接近 0）和
            # 合理的短文档（有实际文字内容）
            if not _pymupdf_output_too_sparse(pymupdf_text, file_path):
                return pymupdf_text
            # 输出稀疏，可能是图片型 PDF，降级到 MarkItDown
            logger.warning(
                "pymupdf4llm produced only %d chars for %s (likely image-based PDF); falling back to MarkItDown",
                len(pymupdf_text.strip()),
                file_path.name,
            )
        # pymupdf4llm 未安装或 auto 模式降级 → 使用 MarkItDown

    return _convert_with_markitdown(file_path)


async def convert_file_to_markdown(file_path: Path) -> Path | None:
    """将支持的文档文件异步转换为 Markdown。

    这是文档转换模块的公开入口函数。根据文件大小自动选择同步或异步执行：
    - 大文件（> 1 MB）：在后台线程池中执行，避免阻塞事件循环。
    - 小文件（<= 1 MB）：在当前线程同步执行，减少调度开销。

    转换成功后，会在原文件同目录下生成同名的 ``.md`` 文件。

    Args:
        file_path: 待转换的文件路径。文件格式必须在
            ``CONVERTIBLE_EXTENSIONS`` 集合中。

    Returns:
        生成的 ``.md`` 文件的 ``Path`` 对象。
        转换失败时返回 ``None``（不抛出异常，错误记录在日志中）。
    """
    try:
        pdf_converter = _get_pdf_converter()
        file_size = file_path.stat().st_size

        if file_size > _ASYNC_THRESHOLD_BYTES:
            # 大文件在后台线程中转换，避免阻塞 asyncio 事件循环
            text = await asyncio.to_thread(_do_convert, file_path, pdf_converter)
        else:
            # 小文件同步执行，避免线程调度的额外开销
            text = _do_convert(file_path, pdf_converter)

        md_path = file_path.with_suffix(".md")
        md_path.write_text(text, encoding="utf-8")

        logger.info("Converted %s to markdown: %s (%d chars)", file_path.name, md_path.name, len(text))
        return md_path
    except Exception as e:
        # 转换失败不抛出异常，记录错误日志并返回 None，
        # 让调用者可以优雅降级（如仅展示原始文件链接）
        logger.error("Failed to convert %s to markdown: %s", file_path.name, e)
        return None


# ===================== 文档大纲提取 =====================
# 以下正则表达式和函数用于从 pymupdf4llm 生成的 Markdown 中
# 提取文档标题层级结构（大纲），供 Agent 快速了解文档结构。

# 样式 1：加粗结构性标题的正则。
# 匹配整行为单个 **...** 块且内容以已知结构性关键词开头的行。
# 用于识别 SEC 文件中使用加粗+大写区分的章节标题（与正文使用相同字号，
# pymupdf4llm 无法将其提升为 # 标题）。
#
# 匹配要求（全部满足）：
#   1. 整行是单个 **...** 块（无其他正文）
#   2. 以已知结构性关键词开头：
#      - ITEM / PART / SECTION（后跟可选的数字/字母编号）
#      - SCHEDULE, EXHIBIT, APPENDIX, ANNEX, CHAPTER
#   排除全大写地址和模板文本（如 "CURRENT REPORT"、"SIGNATURES"），
#   因为它们不以这些关键词开头。
#
# 中文标题（如"第三节..."）已被 pymupdf4llm 正确识别为 # 标题，
# 无需此正则处理。
_BOLD_HEADING_RE = re.compile(r"^\*\*((ITEM|PART|SECTION|SCHEDULE|EXHIBIT|APPENDIX|ANNEX|CHAPTER)\b[A-Z0-9 .,\-]*)\*\*\s*$")

# 样式 2：分离式加粗标题的正则。
# 匹配 pymupdf4llm 在章节编号和标题文本属于不同 PDF span 时产生的格式，
# 例如 ``**1** **Introduction**`` 或 ``**3.2** **Multi-Head Attention**``。
#
# 匹配要求：
#   1. 整行仅由 **...** 块和空白组成（无正文）
#   2. 第一个块为章节编号（数字和点，如 "1"、"3.2"、"A.1"）
#   3. 第二个块不能为纯数字/标点 —— 排除财务报表表头如 **2023** **2022**，
#      但允许非 ASCII 标题如 **1** **概述**（使用负向前瞻而非 [A-Za-z]）
#   4. 最多两个额外块（共四个），使用 [^*]+ 避免正则内部出现 *，
#      保持线性复杂度，防止 ReDoS 攻击
_SPLIT_BOLD_HEADING_RE = re.compile(r"^\*\*[\dA-Z][\d\.]*\*\*\s+\*\*(?!\d[\d\s.,\-–—/:()%]*\*\*)[^*]+\*\*(?:\s+\*\*[^*]+\*\*){0,2}\s*$")

# 注入到 Agent 上下文中的大纲条目数量上限。
# 即使文档很长，也只取前 N 个标题，防止上下文 token 消耗过高。
MAX_OUTLINE_ENTRIES = 50

# 允许的 PDF 转换引擎配置值集合
_ALLOWED_PDF_CONVERTERS = {"auto", "pymupdf4llm", "markitdown"}


def _clean_bold_title(raw: str) -> str:
    """清理可能包含 pymupdf4llm 加粗残留的标题文本。

    pymupdf4llm 有时将相邻的加粗 span 输出为 ``**A** **B**`` 而非
    单个 ``**A B**`` 块。本函数合并这些片段，然后剥离最外层的 ``**...**``
    包裹，返回纯文本标题。

    Args:
        raw: 待清理的标题原始字符串。

    Returns:
        清理后的纯文本标题。

    Examples::

        _clean_bold_title("**Overview**")
        # → "Overview"

        _clean_bold_title("**UNITED STATES** **SECURITIES**")
        # → "UNITED STATES SECURITIES"

        _clean_bold_title("plain text")
        # → "plain text"（无变化）
    """
    # 合并相邻的加粗片段："** **" → " "
    merged = re.sub(r"\*\*\s*\*\*", " ", raw).strip()
    # 如果整个字符串被 **...** 包裹，剥离外层包裹
    if m := re.fullmatch(r"\*\*(.+?)\*\*", merged, re.DOTALL):
        return m.group(1).strip()
    return merged


def extract_outline(md_path: Path) -> list[dict]:
    """从 Markdown 文件中提取文档大纲（标题层级结构）。

    识别 pymupdf4llm 生成的三种标题样式：

    1. **标准 Markdown 标题** —— 以 ``#`` 开头的行。
       行内的 ``**...**`` 包裹和相邻加粗片段（``** **``）会被清理，
       确保标题为纯文本。

    2. **加粗结构性标题** —— ``**ITEM 1. BUSINESS**``、``**PART II**`` 等。
       SEC 文件使用加粗+大写标记章节标题，但字号与正文相同，
       pymupdf4llm 无法将其提升为 ``#`` 标题。

    3. **分离式加粗标题** —— ``**1** **Introduction**``、``**3.2** **Attention**``。
       当 PDF 中章节编号和标题文本属于不同的文本 span 时，
       pymupdf4llm 会输出这种格式（常见于学术论文）。

    Args:
        md_path: Markdown 文件路径。

    Returns:
        字典列表，每个字典包含：
        - ``title`` (str): 标题纯文本
        - ``line`` (int): 标题在文件中的行号（从 1 开始）
        当大纲超过 ``MAX_OUTLINE_ENTRIES`` 条时，末尾追加一个哨兵条目
        ``{"truncated": True}``，调用者可据此渲染"显示前 N 个标题"提示。
        文件无法读取或无标题时返回空列表。
    """
    outline: list[dict] = []
    try:
        with md_path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped:
                    continue

                # 样式 1：标准 Markdown 标题（# 开头）
                if stripped.startswith("#"):
                    # 清理行内可能存在的 **...** 包裹
                    title = _clean_bold_title(stripped.lstrip("#").strip())
                    if title:
                        outline.append({"title": title, "line": lineno})

                # 样式 2：单个加粗块，内容以 SEC 结构性关键词开头
                elif m := _BOLD_HEADING_RE.match(stripped):
                    title = m.group(1).strip()
                    if title:
                        outline.append({"title": title, "line": lineno})

                # 样式 3：分离式加粗标题 —— **<编号>** **<标题>**
                # 正则已限制最多 4 个块且第二个块非纯数字
                elif _SPLIT_BOLD_HEADING_RE.match(stripped):
                    # 提取所有 **...** 块中的文本，用空格连接
                    title = " ".join(re.findall(r"\*\*([^*]+)\*\*", stripped))
                    if title:
                        outline.append({"title": title, "line": lineno})

                # 达到上限后追加截断哨兵并停止扫描
                if len(outline) >= MAX_OUTLINE_ENTRIES:
                    outline.append({"truncated": True})
                    break
    except Exception:
        return []

    return outline


def _get_uploads_config_value(key: str, default: object) -> object:
    """从应用配置中读取 uploads 相关配置值。

    兼容字典和属性两种配置访问方式（取决于配置对象的类型）。

    Args:
        key: 配置键名。
        default: 键不存在时的默认值。

    Returns:
        配置值或默认值。
    """
    cfg = get_app_config()
    uploads_cfg = getattr(cfg, "uploads", None)
    if isinstance(uploads_cfg, dict):
        return uploads_cfg.get(key, default)
    return getattr(uploads_cfg, key, default)


def _get_pdf_converter() -> str:
    """从应用配置中读取 PDF 转换引擎设置，默认为 ``"auto"``。

    执行以下处理：
    1. 读取配置值并转为小写字符串。
    2. 校验值是否在允许集合 ``{"auto", "pymupdf4llm", "markitdown"}`` 中。
    3. 无效值回退到 ``"auto"`` 并记录警告日志。
    4. 任何异常（如配置对象结构异常）也回退到 ``"auto"``。

    Returns:
        有效的 PDF 转换引擎名称字符串。
    """
    try:
        raw = str(_get_uploads_config_value("pdf_converter", "auto")).strip().lower()
        if raw not in _ALLOWED_PDF_CONVERTERS:
            # 配置值无效（如拼写错误），记录警告并回退到 auto
            logger.warning("Invalid pdf_converter value %r; falling back to 'auto'", raw)
            return "auto"
        return raw
    except Exception:
        pass
    return "auto"
