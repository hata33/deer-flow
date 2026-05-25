"""
DuckDuckGo 搜索模块 — 基于 DuckDuckGo 的免费网络搜索

本模块通过 DuckDuckGo 搜索引擎提供网络搜索功能，无需 API Key。
使用 ddgs 库与 DuckDuckGo 的搜索接口交互。

优势:
    - 无需 API Key，开箱即用
    - 支持安全搜索和区域设置
    - 延迟低，适合快速信息检索

导出:
    - web_search_tool: LangChain 注册的网络搜索工具函数
"""

from .tools import web_search_tool

__all__ = ["web_search_tool"]
