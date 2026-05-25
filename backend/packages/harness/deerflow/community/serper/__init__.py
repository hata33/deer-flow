"""
Serper 搜索模块 — 基于 Serper (Google Search API) 的网络搜索

本模块通过 Serper API 提供实时 Google 搜索结果。Serper 是一个
轻量级的 Google 搜索 JSON API 服务，需要 API Key 才能使用。

特性:
    - 实时 Google 搜索结果
    - 低延迟 JSON API
    - 支持环境变量和配置文件两种 API Key 设置方式

导出:
    - web_search_tool: LangChain 注册的网络搜索工具函数
"""

from .tools import web_search_tool

__all__ = ["web_search_tool"]
