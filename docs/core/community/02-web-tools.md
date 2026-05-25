# 网页工具详解

> 源码路径：`backend/packages/harness/deerflow/community/`

网页工具专注于**网页内容抓取和结构化提取**，将原始 HTML 转换为 Agent 可理解的纯文本或 Markdown 格式。DeerFlow 提供三种网页工具实现。

---

## 1. Jina AI Reader

> 源码：`community/jina_ai/jina_client.py`、`community/jina_ai/tools.py`

### 概述

Jina AI Reader 是基于 Jina 的网页内容提取服务，通过 `r.jina.ai` 端点将任意网页转换为结构化 HTML，再由 DeerFlow 内置的 `ReadabilityExtractor` 进行二次提取，输出干净的 Markdown。

### 架构

```
Agent 调用 web_fetch_tool(url)
        │
        ▼
  JinaClient.crawl(url) ──► r.jina.ai API ──► 原始 HTML
        │
        ▼
  ReadabilityExtractor.extract_article(html) ──► Readability 文章对象
        │
        ▼
  article.to_markdown()[:4096] ──► Markdown 文本（截断）
```

### JinaClient

> 文件：`community/jina_ai/jina_client.py`

```python
class JinaClient:
    async def crawl(self, url: str, return_format: str = "html", timeout: int = 10) -> str
```

**请求配置**：

| 请求头 | 值 | 说明 |
|:-------|:---|:-----|
| `Content-Type` | `application/json` | 固定 |
| `X-Return-Format` | `html` | 返回格式（默认 HTML） |
| `X-Timeout` | `10` | 请求超时（秒） |
| `Authorization` | `Bearer $JINA_API_KEY` | 可选，提供 API Key 可获得更高速率限制 |

**API Key 策略**：
- 未设置 `JINA_API_KEY` 环境变量时仍然可用（使用免费额度）
- 首次缺失时打印一次警告日志（通过 `_api_key_warned` 标志控制）
- 设置 API Key 后享有更高速率限制

**请求端点**：`POST https://r.jina.ai/`

**请求体**：`{"url": url}`

**错误处理**：

| 场景 | 返回值 |
|:-----|:-------|
| 非 200 状态码 | `"Error: Jina API returned status {code}: {text}"` |
| 空响应 | `"Error: Jina API returned empty response"` |
| 网络异常 | `"Error: Request to Jina API failed: {Type}: {msg}"` |

### web_fetch_tool

> 文件：`community/jina_ai/tools.py`

```python
@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL."""
```

**特性**：这是社区工具中唯一的**异步工具**（`async def`），使用 `httpx.AsyncClient` 进行网络请求。

**参数**：

| 参数 | 类型 | 说明 |
|:-----|:-----|:-----|
| `url` | `str` | 目标网页 URL（必须包含 schema） |

**配置项**（通过 `config.yaml` 的 `web_fetch` 节）：

| 配置键 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `timeout` | `10` | 请求超时（秒） |

**内部流程**：
1. 创建 `JinaClient` 实例
2. `await jina_client.crawl(url, return_format="html", timeout=...)` 获取 HTML
3. 如果返回以 `"Error:"` 开头，直接返回错误
4. `await asyncio.to_thread(readability_extractor.extract_article, html_content)` 在线程池中执行 Readability 提取
5. `article.to_markdown()[:4096]` 截断到 4KB

**内容长度限制**：Markdown 输出截断到 **4096 字符**。

### 配置示例

```yaml
# config.yaml
tools:
  - name: web_fetch
    use: deerflow.community.jina_ai.tools:web_fetch_tool

web_fetch:
  timeout: 15
```

### 使用场景

- 需要高质量文章正文提取（去除导航栏、广告等）
- 不需要 API Key 即可使用的基础网页抓取
- 需要异步处理的场景

### 依赖

```
pip install httpx
# ReadabilityExtractor 为 DeerFlow 内置模块，无需额外安装
```

---

## 2. Firecrawl

> 源码：`community/firecrawl/tools.py`

### 概述

Firecrawl 提供专业的网页抓取服务，支持搜索和结构化 Markdown 抓取。通过 `FirecrawlApp` SDK 访问 Firecrawl API。

### 工具列表

| 工具名 | LangChain 注册名 | 功能 |
|:-------|:------------------|:-----|
| `web_search_tool` | `web_search` | 网页搜索 |
| `web_fetch_tool` | `web_fetch` | 结构化网页抓取 |

### web_search_tool

```python
@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web."""
```

**配置项**：

| 配置键 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `api_key` | 必填 | Firecrawl API Key |
| `max_results` | `5` | 最大返回结果数 |

**内部流程**：
1. `_get_firecrawl_client("web_search")` 创建 `FirecrawlApp` 实例
2. `client.search(query, limit=max_results)` 执行搜索
3. 从 `result.web`（`SearchResultWeb` 对象列表）提取结果
4. 标准化为 `{title, url, snippet}` 格式

### web_fetch_tool

```python
@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL."""
```

**内部流程**：
1. `_get_firecrawl_client("web_fetch")` 创建 `FirecrawlApp` 实例
2. `client.scrape(url, formats=["markdown"])` 抓取网页并转换为 Markdown
3. 从 `result.markdown` 获取 Markdown 内容
4. 从 `result.metadata.title` 获取页面标题
5. 返回 `# {title}\n\n{markdown_content[:4096]}`

**内容长度限制**：Markdown 输出截断到 **4096 字符**。

**错误处理**：

| 场景 | 返回值 |
|:-----|:-------|
| 搜索/抓取异常 | `"Error: {exception_message}"` |
| 抓取无内容 | `"Error: No content found"` |

### 配置示例

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.firecrawl.tools:web_search_tool
  - name: web_fetch
    use: deerflow.community.firecrawl.tools:web_fetch_tool

web_search:
  api_key: $FIRECRAWL_API_KEY
  max_results: 10
web_fetch:
  api_key: $FIRECRAWL_API_KEY
```

### 使用场景

- 需要专业级网页抓取（JavaScript 渲染页面）
- 需要同一平台同时提供搜索和抓取能力
- 对抓取质量要求高的生产环境

### 依赖

```
pip install firecrawl-py
```

---

## 3. InfoQuest（BytePlus）

> 源码：`community/infoquest/infoquest_client.py`、`community/infoquest/tools.py`

### 概述

InfoQuest 是 BytePlus 提供的综合搜索和抓取服务，支持网页搜索、网页抓取和图像搜索三大功能。一个客户端即可覆盖搜索 + 抓取 + 图像搜索的完整需求。

文档地址：https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest

### InfoQuestClient

> 文件：`community/infoquest/infoquest_client.py`

```python
class InfoQuestClient:
    def __init__(self, fetch_time, fetch_timeout, fetch_navigation_timeout,
                 search_time_range, image_search_time_range, image_size)
```

**构造参数**：

| 参数 | 默认值 | 说明 |
|:-----|:-------|:-----|
| `fetch_time` | `-1` | 抓取时间（-1 表示不限制） |
| `fetch_timeout` | `-1` | 抓取超时（秒） |
| `fetch_navigation_timeout` | `-1` | 导航超时（秒） |
| `search_time_range` | `-1` | 搜索时间范围（天） |
| `image_search_time_range` | `-1` | 图像搜索时间范围（1-365 天） |
| `image_size` | `"i"` | 图像尺寸（l/m/i） |

**API Key**：通过环境变量 `INFOQUEST_API_KEY` 设置，缺失时打印警告但不会阻止请求。

**API 端点**：

| 功能 | 端点 |
|:-----|:-----|
| 网页搜索 | `POST https://search.infoquest.bytepluses.com` |
| 网页抓取 | `POST https://reader.infoquest.bytepluses.com` |
| 图像搜索 | `POST https://search.infoquest.bytepluses.com`（`search_type: "Images"`） |

### 工具列表

| 工具名 | LangChain 注册名 | 功能 |
|:-------|:------------------|:-----|
| `web_search_tool` | `web_search` | 网页搜索 |
| `web_fetch_tool` | `web_fetch` | 网页内容抓取 |
| `image_search_tool` | `image_search` | 图像搜索 |

### web_fetch_tool

**内部流程**：
1. `client.fetch(url)` 调用 InfoQuest Reader API
2. 响应解析优先级：`reader_result` > `content` > 原始文本
3. 通过 `ReadabilityExtractor.extract_article()` 进行文章提取
4. `article.to_markdown()[:4096]` 截断到 4KB

### 配置示例

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.infoquest.tools:web_search_tool
  - name: web_fetch
    use: deerflow.community.infoquest.tools:web_fetch_tool
  - name: image_search
    use: deerflow.community.infoquest.tools:image_search_tool

web_search:
  search_time_range: 30     # 最近 30 天
web_fetch:
  timeout: 15
  navigation_timeout: 10
  fetch_time: 5
image_search:
  image_search_time_range: 7
  image_size: l
```

### 使用场景

- 需要一站式搜索 + 抓取 + 图像搜索
- BytePlus 生态用户
- 需要精细时间范围过滤的搜索

### 依赖

```
pip install requests
```

---

## 工具对比总览

| 特性 | Jina AI | Firecrawl | InfoQuest |
|:-----|:--------|:----------|:----------|
| **网页抓取** | 是 | 是 | 是 |
| **网页搜索** | 否 | 是 | 是 |
| **图像搜索** | 否 | 否 | 是 |
| **API Key** | 可选 | 必须 | 可选 |
| **输出格式** | Markdown | Markdown | Markdown |
| **内容截断** | 4096 字符 | 4096 字符 | 4096 字符 |
| **异步支持** | 是（async） | 否（sync） | 否（sync） |
| **Readability 提取** | 是 | 否（服务端处理） | 是 |
| **提供商** | Jina AI | Firecrawl | BytePlus |

## 通用设计模式

### Readability 二次提取

Jina AI 和 InfoQuest 的 `web_fetch_tool` 都使用 DeerFlow 内置的 `ReadabilityExtractor`（`deerflow.utils.readability`）对抓取到的 HTML 进行文章正文提取：

```python
from deerflow.utils.readability import ReadabilityExtractor

readability_extractor = ReadabilityExtractor()
article = readability_extractor.extract_article(html_content)
markdown = article.to_markdown()[:4096]
```

这一步骤的作用是：
1. 去除导航栏、页脚、广告等非正文内容
2. 提取文章标题、正文、作者等结构化信息
3. 转换为干净的 Markdown 格式

### 4096 字符截断

所有网页工具的输出都截断到 4096 字符。这个限制是为了：
- 控制 LLM 上下文窗口占用
- 防止超长网页内容淹没其他工具输出
- 保持工具响应在合理的大小范围内

如果需要获取完整内容，Agent 可以通过多次调用或调整配置来实现。
