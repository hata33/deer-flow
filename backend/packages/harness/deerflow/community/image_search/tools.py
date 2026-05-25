"""
DuckDuckGo 图片搜索工具 — 用于图像生成参考的图片搜索

本模块通过 DuckDuckGo 图片搜索引擎提供图片搜索功能。
主要设计用于 AI 图像生成流程中的参考图查找。

核心用途:
    在生成图像之前，先搜索相关的参考图片，以提高生成质量和视觉准确性。
    返回的图片 URL 可以直接作为图像生成工具的参考输入。

支持的过滤选项:
    - size: 图片尺寸（Small/Medium/Large/Wallpaper）
    - type_image: 图片类型（photo/clipart/gif/transparent/line）
    - layout: 布局（Square/Tall/Wide）
    - color: 颜色过滤
    - license_image: 许可证过滤

依赖:
    - ddgs: DuckDuckGo 搜索的 Python 客户端库

配置方式:
    在 config.yaml 的 tools 段下配置:
        tools:
          image_search:
            max_results: 5

设计决策:
    - 搜索结果包含 usage_hint 字段，指导用户如何使用返回的图片
    - 结果标准化为 {title, image_url, thumbnail_url} 格式
    - 使用 DuckDuckGo 无需 API Key，降低了使用门槛
"""

import json
import logging

from langchain.tools import tool

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)


def _search_images(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    size: str | None = None,
    color: str | None = None,
    type_image: str | None = None,
    layout: str | None = None,
    license_image: str | None = None,
) -> list[dict]:
    """使用 DuckDuckGo 执行图片搜索。

    通过 ddgs 库调用 DuckDuckGo 图片搜索 API，获取与查询关键词
    匹配的图片结果。支持多种过滤选项以精确搜索结果。

    Args:
        query: 搜索关键词字符串。
        max_results: 最大返回结果数量，默认为 5。
        region: 搜索区域设置，默认为 "wt-wt"（全球）。
        safesearch: 安全搜索级别，默认为 "moderate"。
        size: 图片尺寸过滤，可选值:
            "Small", "Medium", "Large", "Wallpaper"
        color: 颜色过滤选项。
        type_image: 图片类型过滤，可选值:
            "photo", "clipart", "gif", "transparent", "line"
        layout: 布局过滤，可选值: "Square", "Tall", "Wide"。
        license_image: 许可证类型过滤。

    Returns:
        图片搜索结果字典列表。每个字典包含 title、thumbnail、image 等字段。
        如果搜索失败或未安装 ddgs 库，返回空列表。
    """
    try:
        from ddgs import DDGS
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []

    ddgs = DDGS(timeout=30)

    try:
        # 构建搜索参数，仅包含非 None 的可选参数
        kwargs = {
            "region": region,
            "safesearch": safesearch,
            "max_results": max_results,
        }

        if size:
            kwargs["size"] = size
        if color:
            kwargs["color"] = color
        if type_image:
            kwargs["type_image"] = type_image
        if layout:
            kwargs["layout"] = layout
        if license_image:
            kwargs["license_image"] = license_image

        results = ddgs.images(query, **kwargs)
        return list(results) if results else []

    except Exception as e:
        logger.error(f"Failed to search images: {e}")
        return []


@tool("image_search", parse_docstring=True)
def image_search_tool(
    query: str,
    max_results: int = 5,
    size: str | None = None,
    type_image: str | None = None,
    layout: str | None = None,
) -> str:
    """在线搜索图片。在图像生成之前使用此工具查找角色、肖像、物品、场景或任何需要视觉准确性的内容的参考图片。

    **使用时机:**
    - 生成角色/肖像图片前：搜索相似的姿势、表情、风格
    - 生成特定物品/产品图片前：搜索准确的视觉参考
    - 生成场景/地点图片前：搜索建筑或环境参考
    - 生成时尚/服装图片前：搜索风格和细节参考

    返回的图片 URL 可以作为图像生成中的参考图片，显著提高生成质量。

    Args:
        query: 搜索关键词，描述要查找的图片。更具体的关键词效果更好
               （例如使用 "Japanese woman street photography 1990s" 而非 "woman"）。
        max_results: 返回图片的最大数量。默认为 5。
        size: 图片尺寸过滤。可选值: "Small", "Medium", "Large", "Wallpaper"。
              参考图片建议使用 "Large"。
        type_image: 图片类型过滤。可选值: "photo", "clipart", "gif", "transparent", "line"。
                    真实参考图片建议使用 "photo"。
        layout: 布局过滤。可选值: "Square", "Tall", "Wide"。根据生成需求选择。

    Returns:
        JSON 格式的图片搜索结果，包含:
        - query: 原始搜索查询
        - total_results: 结果总数
        - results: 结果列表，每条包含 title、image_url 和 thumbnail_url
        - usage_hint: 使用提示，说明如何将结果用于图像生成
    """
    config = get_app_config().get_tool_config("image_search")

    # 如果配置中设置了 max_results，则覆盖默认值
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results", max_results)

    results = _search_images(
        query=query,
        max_results=max_results,
        size=size,
        type_image=type_image,
        layout=layout,
    )

    if not results:
        return json.dumps({"error": "No images found", "query": query}, ensure_ascii=False)

    # 将搜索结果标准化为统一格式
    normalized_results = [
        {
            "title": r.get("title", ""),
            "image_url": r.get("thumbnail", ""),
            "thumbnail_url": r.get("thumbnail", ""),
        }
        for r in results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
        "usage_hint": "Use the 'image_url' values as reference images in image generation. Download them first if needed.",
    }

    return json.dumps(output, indent=2, ensure_ascii=False)
