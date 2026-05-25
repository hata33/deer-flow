"""
InfoQuest 工具 — 基于 BytePlus InfoQuest 的搜索、抓取和图片搜索

本模块使用 InfoQuest 客户端提供三种核心工具：

提供的工具:
    - web_search_tool: 网络搜索，返回网页和新闻结果
    - web_fetch_tool: 网页内容抓取，使用 Readability 提取正文
    - image_search_tool: 图片搜索，返回图片 URL 列表

配置方式:
    在 config.yaml 的 tools 段下配置:
        tools:
          web_search:
            search_time_range: -1
          web_fetch:
            fetch_time: -1
            timeout: -1
            navigation_timeout: -1
          image_search:
            image_search_time_range: -1
            image_size: "i"

    环境变量:
        export INFOQUEST_API_KEY="your-api-key"

处理流程:
    - 网络搜索: InfoQuestClient.web_search() → 直接返回 JSON 结果
    - 网页抓取: InfoQuestClient.fetch() → Readability 提取 → Markdown 格式
    - 图片搜索: InfoQuestClient.image_search() → 直接返回 JSON 结果

设计决策:
    - 使用 LangChain 的 @tool 装饰器注册所有三种工具
    - 网页抓取结果通过 ReadabilityExtractor 转换为干净的 Markdown
    - 工具级别的配置从应用配置中动态读取
    - 每次工具调用都创建新的客户端实例（无状态设计）
"""

from langchain.tools import tool

from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

from .infoquest_client import InfoQuestClient

# Readability 内容提取器实例（用于将 HTML 转换为 Markdown）
readability_extractor = ReadabilityExtractor()


def _get_infoquest_client() -> InfoQuestClient:
    """从应用配置创建配置好的 InfoQuest 客户端实例。

    从 web_search、web_fetch 和 image_search 三个配置段中
    读取各自的参数，构建完整的客户端配置。

    Returns:
        配置好的 InfoQuestClient 实例。
    """
    # 读取网络搜索配置
    search_config = get_app_config().get_tool_config("web_search")
    search_time_range = -1
    if search_config is not None and "search_time_range" in search_config.model_extra:
        search_time_range = search_config.model_extra.get("search_time_range")

    # 读取网页抓取配置
    fetch_config = get_app_config().get_tool_config("web_fetch")
    fetch_time = -1
    if fetch_config is not None and "fetch_time" in fetch_config.model_extra:
        fetch_time = fetch_config.model_extra.get("fetch_time")
    fetch_timeout = -1
    if fetch_config is not None and "timeout" in fetch_config.model_extra:
        fetch_timeout = fetch_config.model_extra.get("timeout")
    navigation_timeout = -1
    if fetch_config is not None and "navigation_timeout" in fetch_config.model_extra:
        navigation_timeout = fetch_config.model_extra.get("navigation_timeout")

    # 读取图片搜索配置
    image_search_config = get_app_config().get_tool_config("image_search")
    image_search_time_range = -1
    if image_search_config is not None and "image_search_time_range" in image_search_config.model_extra:
        image_search_time_range = image_search_config.model_extra.get("image_search_time_range")
    image_size = "i"
    if image_search_config is not None and "image_size" in image_search_config.model_extra:
        image_size = image_search_config.model_extra.get("image_size")

    return InfoQuestClient(
        search_time_range=search_time_range,
        fetch_timeout=fetch_timeout,
        fetch_navigation_timeout=navigation_timeout,
        fetch_time=fetch_time,
        image_search_time_range=image_search_time_range,
        image_size=image_size,
    )


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """搜索网络。

    使用 InfoQuest 搜索 API 执行网络搜索，返回包含网页和新闻
    的搜索结果。

    Args:
        query: 搜索查询字符串。

    Returns:
        JSON 格式的搜索结果字符串。
    """

    client = _get_infoquest_client()
    return client.web_search(query)


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """抓取给定 URL 的网页内容。
    仅抓取用户直接提供的 URL 或 web_search 和 web_fetch 工具返回的 URL。
    无法访问需要身份验证的内容（如私有 Google 文档或登录墙后的页面）。
    不要为没有 www. 的 URL 添加 www.。
    URL 必须包含协议：https://example.com 是有效的 URL，而 example.com 是无效的 URL。

    通过 InfoQuest Reader API 获取网页 HTML，然后使用 Readability
    提取器转换为干净的 Markdown 格式。

    Args:
        url: 要抓取内容的网页 URL。

    Returns:
        Markdown 格式的网页内容，截断为 4096 字符。
        如果抓取失败，返回错误信息字符串。
    """
    client = _get_infoquest_client()
    result = client.fetch(url)
    # 检查是否返回了错误
    if result.startswith("Error: "):
        return result
    # 使用 Readability 提取正文内容并转换为 Markdown
    article = readability_extractor.extract_article(result)
    return article.to_markdown()[:4096]


@tool("image_search", parse_docstring=True)
def image_search_tool(query: str) -> str:
    """在线搜索图片。在图像生成之前使用此工具查找角色、肖像、物品、场景或任何需要视觉准确性的内容的参考图片。

    **使用时机:**
    - 生成角色/肖像图片前：搜索相似的姿势、表情、风格
    - 生成特定物品/产品图片前：搜索准确的视觉参考
    - 生成场景/地点图片前：搜索建筑或环境参考
    - 生成时尚/服装图片前：搜索风格和细节参考

    返回的图片 URL 可以作为图像生成中的参考图片，显著提高生成质量。

    Args:
        query: 搜索图片的查询关键词。

    Returns:
        JSON 格式的图片搜索结果字符串。
    """
    client = _get_infoquest_client()
    return client.image_search(query)
