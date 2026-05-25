"""
Serper 网络搜索工具 — 基于 Google Search API 的实时搜索

本模块使用 Serper (https://serper.dev) 提供实时 Google 搜索结果。
Serper 是一个轻量级的 Google 搜索 JSON API，具有低延迟和高质量的特点。

API Key 获取:
    在 https://serper.dev 注册并获取 API Key。

配置方式:
    方式一：在 config.yaml 中配置:
        tools:
          web_search:
            api_key: "your-serper-api-key"
            max_results: 5

    方式二：通过环境变量设置:
        export SERPER_API_KEY="your-serper-api-key"

特性:
    - 实时 Google 搜索结果
    - 支持最大结果数配置
    - API Key 缺失时仅警告一次（避免日志噪音）
    - HTTP 错误处理完善，返回结构化错误信息

设计决策:
    - 使用 httpx 作为 HTTP 客户端（支持同步和异步）
    - API Key 优先从配置文件获取，回退到环境变量
    - 搜索结果标准化为 {title, url, content} 格式
    - 使用全局标志 _api_key_warned 确保缺失 Key 的警告仅打印一次
"""

import json
import logging
import os

import httpx
from langchain.tools import tool

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

# Serper API 端点地址
_SERPER_ENDPOINT = "https://google.serper.dev/search"
# API Key 缺失警告标志，确保只警告一次
_api_key_warned = False


def _get_api_key() -> str | None:
    """获取 Serper API Key。

    优先从应用配置中获取 API Key，如果未配置则回退到
    SERPER_API_KEY 环境变量。

    Returns:
        API Key 字符串，如果都未配置则返回 None。
    """
    config = get_app_config().get_tool_config("web_search")
    if config is not None:
        api_key = config.model_extra.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return api_key
    return os.getenv("SERPER_API_KEY")


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """使用 Serper 的 Google 搜索搜索网络信息。

    通过 Serper API 调用 Google 搜索引擎，获取实时搜索结果。
    需要有效的 Serper API Key（通过配置或环境变量提供）。

    Args:
        query: 搜索关键词。更具体的关键词能获得更好的结果。
        max_results: 返回搜索结果的最大数量。默认为 5。

    Returns:
        JSON 格式的搜索结果，包含:
        - query: 原始搜索查询
        - total_results: 结果总数
        - results: 结果列表，每条包含 title、url 和 content
        如果 API Key 未配置或请求失败，返回包含 error 字段的 JSON。
    """
    global _api_key_warned

    # 从配置中获取 max_results 覆盖值
    config = get_app_config().get_tool_config("web_search")
    if config is not None and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results", max_results)

    # 检查 API Key 是否已配置
    api_key = _get_api_key()
    if not api_key:
        if not _api_key_warned:
            _api_key_warned = True
            logger.warning("Serper API key is not set. Set SERPER_API_KEY in your environment or provide api_key in config.yaml. Sign up at https://serper.dev")
        return json.dumps(
            {"error": "SERPER_API_KEY is not configured", "query": query},
            ensure_ascii=False,
        )

    # 构建请求
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": max_results}

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(_SERPER_ENDPOINT, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Serper API returned HTTP {e.response.status_code}: {e.response.text}")
        return json.dumps(
            {"error": f"Serper API error: HTTP {e.response.status_code}", "query": query},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Serper search failed: {type(e).__name__}: {e}")
        return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)

    # 提取自然搜索结果（organic results）
    organic = data.get("organic", [])
    if not organic:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    # 将搜索结果标准化为统一格式
    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("link", ""),
            "content": r.get("snippet", ""),
        }
        for r in organic[:max_results]
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)
