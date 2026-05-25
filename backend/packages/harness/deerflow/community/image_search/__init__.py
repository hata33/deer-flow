"""
图片搜索模块 — 基于 DuckDuckGo 的图片搜索

本模块提供图片搜索功能，主要用于在 AI 图像生成之前查找参考图片。
通过 DuckDuckGo 图片搜索 API 查找相关图片，返回缩略图 URL。

使用场景:
    - 在生成角色/肖像图片前，搜索相似姿势、表情、风格的参考图
    - 在生成特定物品/产品图片前，搜索准确的视觉参考
    - 在生成场景/地点图片前，搜索建筑或环境参考

导出:
    - image_search_tool: LangChain 注册的图片搜索工具函数
"""

from .tools import image_search_tool

__all__ = ["image_search_tool"]
