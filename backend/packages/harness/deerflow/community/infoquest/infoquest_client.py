"""
InfoQuest 客户端 — BytePlus InfoQuest 搜索和抓取 API 封装

本模块封装了 BytePlus InfoQuest 服务 API，提供网络搜索、图片搜索
和网页内容抓取功能。InfoQuest 是字节跳动旗下的企业级搜索服务。

API 端点:
    - 搜索: https://search.infoquest.bytepluses.com
    - 抓取: https://reader.infoquest.bytepluses.com

核心功能:
    - web_search: 网络搜索，返回网页和新闻结果
    - image_search: 图片搜索，返回图片 URL
    - fetch: 网页内容抓取，返回 HTML 或文本内容

配置方式:
    环境变量:
        export INFOQUEST_API_KEY="your-api-key"

    在 config.yaml 的 tools 段下配置:
        tools:
          web_search:
            search_time_range: -1    # 搜索时间范围（1-365 天，-1 为不限）
          web_fetch:
            fetch_time: -1           # 抓取等待时间
            timeout: -1              # 抓取超时
            navigation_timeout: -1   # 导航超时
          image_search:
            image_search_time_range: -1  # 图片搜索时间范围
            image_size: "i"              # 图片尺寸: "l"(大), "m"(中), "i"(图标)

设计决策:
    - 所有 API 调用均为同步（使用 requests 库）
    - 错误返回格式统一为 "Error: <信息>" 字符串
    - 搜索结果通过 clean_results 方法进行去重和标准化
    - 支持详细的调试日志，便于问题排查
    - -1 作为默认值表示"不限制"

参考文档:
    https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest
"""

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


class InfoQuestClient:
    """BytePlus InfoQuest 搜索和抓取 API 客户端。

    封装了与 InfoQuest 服务的所有 HTTP 交互，包括网络搜索、
    图片搜索和网页内容抓取。

    使用示例:
        client = InfoQuestClient(search_time_range=7)
        results = client.web_search("最新科技新闻")
        html = client.fetch("https://example.com")
    """

    def __init__(self, fetch_time: int = -1, fetch_timeout: int = -1, fetch_navigation_timeout: int = -1, search_time_range: int = -1, image_search_time_range: int = -1, image_size: str = "i"):
        """初始化 InfoQuest 客户端。

        Args:
            fetch_time: 网页抓取等待时间（秒）。-1 表示使用服务默认值。
            fetch_timeout: 网页抓取超时时间（秒）。-1 表示使用服务默认值。
            fetch_navigation_timeout: 网页抓取导航超时（秒）。-1 表示使用服务默认值。
            search_time_range: 网络搜索时间范围（天），1-365。-1 表示不限时间。
            image_search_time_range: 图片搜索时间范围（天），1-365。-1 表示不限时间。
            image_size: 图片尺寸过滤。可选值:
                - "l": 大图
                - "m": 中图
                - "i": 图标（默认）
        """
        logger.info("\n============================================\n🚀 BytePlus InfoQuest Client Initialization 🚀\n============================================")

        self.fetch_time = fetch_time
        self.fetch_timeout = fetch_timeout
        self.fetch_navigation_timeout = fetch_navigation_timeout
        self.search_time_range = search_time_range
        self.image_search_time_range = image_search_time_range
        self.image_size = image_size
        self.api_key_set = bool(os.getenv("INFOQUEST_API_KEY"))
        if logger.isEnabledFor(logging.DEBUG):
            config_details = (
                f"\n📋 Configuration Details:\n"
                f"├── Fetch time: {fetch_time} {'(Default: No fetch time)' if fetch_time == -1 else '(Custom)'}\n"
                f"├── Fetch Timeout: {fetch_timeout} {'(Default: No fetch timeout)' if fetch_timeout == -1 else '(Custom)'}\n"
                f"├── Navigation Timeout: {fetch_navigation_timeout} {'(Default: No Navigation Timeout)' if fetch_navigation_timeout == -1 else '(Custom)'}\n"
                f"├── Search Time Range: {search_time_range} {'(Default: No Search Time Range)' if search_time_range == -1 else '(Custom)'}\n"
                f"├── Image Search Time Range: {image_search_time_range} {'(Default: No Image Search Time Range)' if image_search_time_range == -1 else '(Custom)'}\n"
                f"├── Image Size: {image_size} {'(Default: Medium)' if image_size == 'm' else '(Custom)'}\n"
                f"└── API Key: {'✅ Configured' if self.api_key_set else '❌ Not set'}"
            )

            logger.debug(config_details)
            logger.debug("\n" + "*" * 70 + "\n")

    def fetch(self, url: str, return_format: str = "html") -> str:
        """抓取指定 URL 的网页内容。

        通过 InfoQuest Reader API 抓取网页内容，支持 HTML 格式返回。
        API 会处理 JavaScript 渲染等复杂场景。

        Args:
            url: 要抓取的网页 URL。
            return_format: 返回格式，默认为 "html"。

        Returns:
            网页内容字符串。如果 API 返回 JSON 格式响应，优先提取
            reader_result 字段，其次提取 content 字段。
            如果抓取失败，返回 "Error: <错误信息>" 格式的字符串。
        """
        if logger.isEnabledFor(logging.DEBUG):
            url_truncated = url[:50] + "..." if len(url) > 50 else url
            logger.debug(
                f"InfoQuest - Fetch API request initiated | "
                f"operation=crawl url | "
                f"url_truncated={url_truncated} | "
                f"has_timeout_filter={self.fetch_timeout > 0} | timeout_filter={self.fetch_timeout} | "
                f"has_fetch_time_filter={self.fetch_time > 0} | fetch_time_filter={self.fetch_time} | "
                f"has_navigation_timeout_filter={self.fetch_navigation_timeout > 0} | navi_timeout_filter={self.fetch_navigation_timeout} | "
                f"request_type=sync"
            )

        # 准备请求头
        headers = self._prepare_headers()

        # 准备请求数据
        data = self._prepare_crawl_request_data(url, return_format)

        logger.debug("Sending crawl request to InfoQuest API")
        try:
            response = requests.post("https://reader.infoquest.bytepluses.com", headers=headers, json=data)

            # 检查 HTTP 状态码
            if response.status_code != 200:
                error_message = f"fetch API returned status {response.status_code}: {response.text}"
                logger.debug("InfoQuest Crawler fetch API return status %d: %s for URL: %s", response.status_code, response.text, url)
                return f"Error: {error_message}"

            # 检查空响应
            if not response.text or not response.text.strip():
                error_message = "no result found"
                logger.debug("InfoQuest Crawler returned empty response for URL: %s", url)
                return f"Error: {error_message}"

            # 尝试将响应解析为 JSON 并提取 reader_result 字段
            try:
                response_data = json.loads(response.text)
                # 优先提取 reader_result 字段
                if "reader_result" in response_data:
                    logger.debug("Successfully extracted reader_result from JSON response")
                    return response_data["reader_result"]
                elif "content" in response_data:
                    # 回退到 content 字段
                    logger.debug("reader_result missing in JSON response, falling back to content field: %s", response_data["content"])
                    return response_data["content"]
                else:
                    # 两个字段都不存在，返回原始响应
                    logger.warning("Neither reader_result nor content field found in JSON response")
            except json.JSONDecodeError:
                # 响应不是 JSON 格式，直接返回原始文本
                logger.debug("Response is not in JSON format, returning as-is")
                return response.text

            # 调试日志：打印部分响应内容
            if logger.isEnabledFor(logging.DEBUG):
                response_sample = response.text[:200] + ("..." if len(response.text) > 200 else "")
                logger.debug("Successfully received response, content length: %d bytes, first 200 chars: %s", len(response.text), response_sample)
            return response.text
        except Exception as e:
            error_message = f"fetch API failed: {str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"

    @staticmethod
    def _prepare_headers() -> dict[str, str]:
        """准备 API 请求头。

        包含 Content-Type 和可选的 Authorization 头。
        API Key 从 INFOQUEST_API_KEY 环境变量获取。

        Returns:
            HTTP 请求头字典。
        """
        headers = {
            "Content-Type": "application/json",
        }

        # 如果有 API Key 则添加到请求头
        if os.getenv("INFOQUEST_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('INFOQUEST_API_KEY')}"
            logger.debug("API key added to request headers")
        else:
            logger.warning("InfoQuest API key is not set. Provide your own key for authentication.")

        return headers

    def _prepare_crawl_request_data(self, url: str, return_format: str) -> dict[str, Any]:
        """准备抓取请求的数据体。

        构建包含 URL、格式和可选超时参数的请求数据。
        仅将值为正数的超时参数包含在请求中。

        Args:
            url: 要抓取的网页 URL。
            return_format: 返回内容格式。

        Returns:
            API 请求数据字典。
        """
        # 规范化返回格式
        if return_format and return_format.lower() == "html":
            normalized_format = "HTML"
        else:
            normalized_format = return_format

        data = {"url": url, "format": normalized_format}

        # 仅在设置为正值时添加超时参数
        timeout_params = {}
        if self.fetch_time > 0:
            timeout_params["fetch_time"] = self.fetch_time
        if self.fetch_timeout > 0:
            timeout_params["timeout"] = self.fetch_timeout
        if self.fetch_navigation_timeout > 0:
            timeout_params["navi_timeout"] = self.fetch_navigation_timeout

        # 记录应用的超时参数
        if timeout_params:
            logger.debug("Applying timeout parameters: %s", timeout_params)
            data.update(timeout_params)

        return data

    def web_search_raw_results(
        self,
        query: str,
        site: str,
        output_format: str = "JSON",
    ) -> dict:
        """获取 InfoQuest 网络搜索 API 的原始结果。

        直接调用搜索 API 并返回未经处理的 JSON 响应。

        Args:
            query: 搜索查询关键词。
            site: 限定搜索的网站域名（空字符串表示不限）。
            output_format: 输出格式，默认为 "JSON"。

        Returns:
            API 返回的原始 JSON 响应字典。

        Raises:
            requests.HTTPError: 如果 API 返回非 200 状态码。
        """
        headers = self._prepare_headers()

        params = {"format": output_format, "query": query}
        # 添加时间范围过滤（如果已配置）
        if self.search_time_range > 0:
            params["time_range"] = self.search_time_range

        # 添加站点限定
        if site != "":
            params["site"] = site

        response = requests.post("https://search.infoquest.bytepluses.com", headers=headers, json=params)
        response.raise_for_status()

        # 调试日志：打印部分响应
        response_json = response.json()
        if logger.isEnabledFor(logging.DEBUG):
            response_sample = json.dumps(response_json)[:200] + ("..." if len(json.dumps(response_json)) > 200 else "")
            logger.debug(f"Search API request completed successfully | service=InfoQuest | status=success | response_sample={response_sample}")

        return response_json

    @staticmethod
    def clean_results(raw_results: list[dict[str, dict[str, dict[str, Any]]]]) -> list[dict]:
        """清洗 InfoQuest 网络搜索 API 的原始结果。

        从原始搜索结果中提取网页结果和新闻结果，进行 URL 去重，
        并统一为标准化的结果格式。

        Args:
            raw_results: API 返回的原始搜索结果列表。

        Returns:
            清洗后的搜索结果列表，每条结果包含:
            - type: "page" 或 "news"
            - title: 标题
            - url: 链接
            - desc/snippet: 描述
            - (新闻) time_frame: 时间范围
            - (新闻) source: 来源
        """
        logger.debug("Processing web-search results")

        seen_urls = set()
        clean_results = []
        counts = {"pages": 0, "news": 0}

        for content_list in raw_results:
            content = content_list["content"]
            results = content["results"]

            # 处理自然搜索结果（organic results）
            if results.get("organic"):
                organic_results = results["organic"]
                for result in organic_results:
                    clean_result = {
                        "type": "page",
                    }
                    if "title" in result:
                        clean_result["title"] = result["title"]
                    if "desc" in result:
                        clean_result["desc"] = result["desc"]
                        clean_result["snippet"] = result["desc"]
                    if "url" in result:
                        clean_result["url"] = result["url"]
                        url = clean_result["url"]
                        # URL 去重：确保每个 URL 只出现一次
                        if isinstance(url, str) and url and url not in seen_urls:
                            seen_urls.add(url)
                            clean_results.append(clean_result)
                            counts["pages"] += 1

            # 处理热门新闻（top_stories）
            if results.get("top_stories"):
                news = results["top_stories"]
                for obj in news["items"]:
                    clean_result = {
                        "type": "news",
                    }
                    if "time_frame" in obj:
                        clean_result["time_frame"] = obj["time_frame"]
                    if "source" in obj:
                        clean_result["source"] = obj["source"]
                    title = obj.get("title")
                    url = obj.get("url")
                    if title:
                        clean_result["title"] = title
                    if url:
                        clean_result["url"] = url
                    # 新闻结果也需要标题和有效 URL 才能包含
                    if title and isinstance(url, str) and url and url not in seen_urls:
                        seen_urls.add(url)
                        clean_results.append(clean_result)
                        counts["news"] += 1
        logger.debug(f"Results processing completed | total_results={len(clean_results)} | pages={counts['pages']} | news_items={counts['news']} | unique_urls={len(seen_urls)}")

        return clean_results

    def web_search(
        self,
        query: str,
        site: str = "",
        output_format: str = "JSON",
    ) -> str:
        """执行网络搜索。

        通过 InfoQuest 搜索 API 执行网络搜索，返回经过清洗和
        标准化的搜索结果。

        Args:
            query: 搜索查询关键词。
            site: 限定搜索的网站域名。空字符串表示不限。
            output_format: 输出格式，默认为 "JSON"。

        Returns:
            JSON 格式的搜索结果字符串。
            如果搜索失败，返回 "Error: <错误信息>" 格式的字符串。
        """
        if logger.isEnabledFor(logging.DEBUG):
            query_truncated = query[:50] + "..." if len(query) > 50 else query
            logger.debug(
                f"InfoQuest - Search API request initiated | "
                f"operation=search webs | "
                f"query_truncated={query_truncated} | "
                f"has_time_filter={self.search_time_range > 0} | time_filter={self.search_time_range} | "
                f"has_site_filter={bool(site)} | site={site} | "
                f"request_type=sync"
            )

        try:
            logger.debug("InfoQuest Web-Search - Executing search with parameters")
            raw_results = self.web_search_raw_results(
                query,
                site,
                output_format,
            )
            if "search_result" in raw_results:
                logger.debug("InfoQuest Web-Search - Successfully extracted search_result from JSON response")
                results = raw_results["search_result"]

                logger.debug("InfoQuest Web-Search - Processing raw search results")
                cleaned_results = self.clean_results(results["results"])

                result_json = json.dumps(cleaned_results, indent=2, ensure_ascii=False)

                logger.debug(f"InfoQuest Web-Search - Search tool execution completed | mode=synchronous | results_count={len(cleaned_results)}")
                return result_json

            elif "content" in raw_results:
                # 回退到 content 字段
                error_message = "web search API return wrong format"
                logger.error("web search API return wrong format, no search_result nor content field found in JSON response, content: %s", raw_results["content"])
                return f"Error: {error_message}"
            else:
                # 两个字段都不存在，返回原始响应
                logger.warning("InfoQuest Web-Search - Neither search_result nor content field found in JSON response")
                return json.dumps(raw_results, indent=2, ensure_ascii=False)

        except Exception as e:
            error_message = f"InfoQuest Web-Search - Search tool execution failed | mode=synchronous | error={str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"

    @staticmethod
    def clean_results_with_image_search(raw_results: list[dict[str, dict[str, dict[str, Any]]]]) -> list[dict]:
        """清洗 InfoQuest 图片搜索 API 的原始结果。

        从原始搜索结果中提取图片 URL，进行去重处理。

        Args:
            raw_results: API 返回的原始图片搜索结果列表。

        Returns:
            清洗后的图片搜索结果列表，每条结果包含:
            - image_url: 原始图片 URL
            - title: 图片标题（可选）
        """
        logger.debug("Processing web-search results")

        seen_urls = set()
        clean_results = []
        counts = {"images": 0}

        for content_list in raw_results:
            content = content_list["content"]
            results = content["results"]

            # 处理图片搜索结果
            if results.get("images_results"):
                images_results = results["images_results"]
                for result in images_results:
                    clean_result = {}
                    if "original" in result:
                        clean_result["image_url"] = result["original"]
                        url = clean_result["image_url"]
                        # URL 去重
                        if isinstance(url, str) and url and url not in seen_urls:
                            seen_urls.add(url)
                            clean_results.append(clean_result)
                            counts["images"] += 1
                    if "title" in result:
                        clean_result["title"] = result["title"]
        logger.debug(f"Results processing completed | total_results={len(clean_results)} | images={counts['images']} | unique_urls={len(seen_urls)}")

        return clean_results

    def image_search_raw_results(
        self,
        query: str,
        site: str = "",
        output_format: str = "JSON",
    ) -> dict:
        """获取 InfoQuest 图片搜索 API 的原始结果。

        调用搜索 API 并指定 search_type 为 Images，获取图片搜索结果。

        Args:
            query: 搜索查询关键词。
            site: 限定搜索的网站域名。空字符串表示不限。
            output_format: 输出格式，默认为 "JSON"。

        Returns:
            API 返回的原始 JSON 响应字典。

        Raises:
            requests.HTTPError: 如果 API 返回非 200 状态码。
        """
        headers = self._prepare_headers()

        # 图片搜索需要指定 search_type
        params = {"format": output_format, "query": query, "search_type": "Images"}

        # 添加时间范围过滤（有效范围 1-365 天）
        if 1 <= self.image_search_time_range <= 365:
            params["time_range"] = self.image_search_time_range
        elif self.image_search_time_range > 0:
            logger.warning(f"time_range {self.image_search_time_range} is out of valid range (1-365), ignoring")

        # 添加站点限定
        if site:
            params["site"] = site

        # 添加图片尺寸过滤
        if self.image_size and self.image_size in ["l", "m", "i"]:
            params["image_size"] = self.image_size
        elif self.image_size:
            logger.warning(f"image_size {self.image_size} is not valid, must be 'l', 'm', or 'i'")

        response = requests.post("https://search.infoquest.bytepluses.com", headers=headers, json=params)
        response.raise_for_status()

        # 调试日志：打印部分响应
        response_json = response.json()
        if logger.isEnabledFor(logging.DEBUG):
            response_sample = json.dumps(response_json)[:200] + ("..." if len(json.dumps(response_json)) > 200 else "")
            logger.debug(f"Image Search API request completed successfully | service=InfoQuest | status=success | response_sample={response_sample}")

        return response_json

    def image_search(
        self,
        query: str,
        site: str = "",
        output_format: str = "JSON",
    ) -> str:
        """执行图片搜索。

        通过 InfoQuest 搜索 API 执行图片搜索，返回经过清洗和
        标准化的图片搜索结果。

        Args:
            query: 搜索查询关键词。
            site: 限定搜索的网站域名。空字符串表示不限。
            output_format: 输出格式，默认为 "JSON"。

        Returns:
            JSON 格式的图片搜索结果字符串，每条结果包含 image_url 和可选的 title。
            如果搜索失败，返回 "Error: <错误信息>" 格式的字符串。
        """
        if logger.isEnabledFor(logging.DEBUG):
            query_truncated = query[:50] + "..." if len(query) > 50 else query
            logger.debug(
                f"InfoQuest - Image Search API request initiated | "
                f"operation=search images | "
                f"query_truncated={query_truncated} | "
                f"has_site_filter={bool(site)} | site={site} | "
                f"image_search_time_range={self.image_search_time_range if self.image_search_time_range >= 1 and self.image_search_time_range <= 365 else 'default'} | "
                f"image_size={self.image_size} |"
                f"request_type=sync"
            )

        try:
            logger.info("InfoQuest Image Search - Executing search with parameters")
            raw_results = self.image_search_raw_results(
                query,
                site,
                output_format,
            )

            if "search_result" in raw_results:
                logger.debug("InfoQuest Image Search - Successfully extracted search_result from JSON response")
                results = raw_results["search_result"]

                logger.debug(f"InfoQuest Image Search - Processing raw image search results: {results}")
                cleaned_results = self.clean_results_with_image_search(results["results"])

                result_json = json.dumps(cleaned_results, indent=2, ensure_ascii=False)

                logger.debug(f"InfoQuest Image Search - Image search tool execution completed | mode=synchronous | results_count={len(cleaned_results)}")
                return result_json

            elif "content" in raw_results:
                # 回退到 content 字段
                error_message = "image search API return wrong format"
                logger.error("image search API return wrong format, no search_result nor content field found in JSON response, content: %s", raw_results["content"])
                return f"Error: {error_message}"
            else:
                # 两个字段都不存在，返回原始响应
                logger.warning("InfoQuest Image Search - Neither search_result nor content field found in JSON response")
                return json.dumps(raw_results, indent=2, ensure_ascii=False)

        except Exception as e:
            error_message = f"InfoQuest Image Search - Image search tool execution failed | mode=synchronous | error={str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"
