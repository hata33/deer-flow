# 06 - 社区工具实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/community/` 目录下的源码，逐层拆解社区工具的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方（外部世界）                           │
│                                                                  │
│  agents/lead_agent/agent.py                                     │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ get_available_tools()                                     │  │
│  │  └─ resolve_variable("deerflow.community.tavily.tools:web_search_tool") │
│  └──────────────────────────┬────────────────────────────────┘  │
│                              │ config.yaml 决定加载哪个实现      │
└──────────────────────────────┼───────────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────────┐
│                   community 包（内部世界）                         │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ 搜索工具（注册为 "web_search"）                              │ │
│  │  tavily/tools.py      ── TavilyClient.search() → JSON       │ │
│  │  serper/tools.py      ── httpx.post(Serper API) → JSON      │ │
│  │  ddg_search/tools.py  ── DDGS.text() → JSON                 │ │
│  │  firecrawl/tools.py   ── FirecrawlApp.search() → JSON       │ │
│  │  exa/tools.py         ── Exa.search() → JSON                │ │
│  │  infoquest/tools.py   ── InfoQuestClient.web_search() → JSON│ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ 抓取工具（注册为 "web_fetch"）                               │ │
│  │  tavily/tools.py      ── TavilyClient.extract()             │ │
│  │  jina_ai/tools.py     ── JinaClient + Readability           │ │
│  │  firecrawl/tools.py   ── FirecrawlApp.scrape(markdown)      │ │
│  │  exa/tools.py         ── Exa.get_contents()                 │ │
│  │  infoquest/tools.py   ── InfoQuestClient.fetch() + Readability│ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ 图片搜索（注册为 "image_search"）                            │ │
│  │  image_search/tools.py ── DDGS.images() → JSON              │ │
│  │  infoquest/tools.py    ── InfoQuestClient.image_search()    │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ 沙箱工具                                                     │ │
│  │  aio_sandbox/  ── Docker 容器化沙箱实现                      │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  共享基础设施:                                                    │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │ config                │  │ utils/readability.py             │ │
│  │ get_tool_config()     │  │ ReadabilityExtractor             │ │
│  └──────────────────────┘  └──────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
```

---

## 二、工具注册机制

### 2.1 @tool 装饰器统一模式

所有社区工具都使用相同的注册模式：

```python
from langchain.tools import tool

@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """搜索网络。
    ...
    Args:
        query: 搜索查询关键词。
    Returns:
        JSON 格式的搜索结果...
    """
    # 实现逻辑
```

**parse_docstring=True**：从 Google-style docstring 自动提取 `Args` 和 `Returns` 部分，生成 JSON Schema。LLM 通过 schema 理解工具参数含义，决定何时调用。

**工具名冲突解决**：多个模块注册同名工具（如都叫 `web_search`），但 `config.yaml` 只加载一个实现：

```yaml
tools:
  - use: "deerflow.community.tavily.tools:web_search_tool"
    group: "web"
```

`resolve_variable()` 通过反射加载指定路径的函数对象，其他同名实现不会被加载。

---

## 三、配置访问模式

### 3.1 get_app_config().get_tool_config()

所有工具通过统一接口读取配置：

```python
config = get_app_config().get_tool_config("web_search")
if config is not None and "api_key" in config.model_extra:
    api_key = config.model_extra.get("api_key")
```

**model_extra 的作用**：`ToolConfig` 是 Pydantic 模型，`model_extra` 包含 schema 未显式声明的额外字段（如 `api_key`、`max_results`、`timeout`）。这种设计允许工具定义任意配置项，无需修改 `ToolConfig` 类。

### 3.2 配置读取模式总览

| 工具 | 配置段 | 读取的配置项 | API Key 来源 |
|------|--------|-------------|-------------|
| Tavily | web_search | api_key, max_results | config → env(TAVILY_API_KEY) |
| Serper | web_search | api_key, max_results | config → env(SERPER_API_KEY) |
| DDG | web_search | max_results | 无需 Key |
| Firecrawl | web_search, web_fetch | api_key, max_results | config |
| Exa | web_search, web_fetch | api_key, max_results, search_type, contents_max_characters | config |
| Jina | web_fetch | timeout | env(JINA_API_KEY, 可选) |
| InfoQuest | web_search, web_fetch, image_search | search_time_range, fetch_time, timeout, image_size | env(INFOQUEST_API_KEY) |

---

## 四、搜索引擎实现详解

### 4.1 Tavily 流程

```
web_search_tool(query)
      │
      ├─ _get_tavily_client()
      │    └─ config.model_extra.get("api_key")
      │    └─ TavilyClient(api_key=api_key)
      │
      ├─ client.search(query, max_results=5)
      │    └─ 返回 {"results": [{"title": ..., "url": ..., "content": ...}]}
      │
      └─ 标准化 → JSON
           [{"title": ..., "url": ..., "snippet": ...}]
```

**Tavily 的 web_fetch**：使用 `client.extract([url])` 直接获取页面内容，返回 `raw_content` 字段。截断为 4096 字符，加上标题前缀。

### 4.2 Serper 流程

```
web_search_tool(query)
      │
      ├─ _get_api_key()
      │    └─ config.api_key ?? env.SERPER_API_KEY
      │    └─ 都没有 → 返回 {"error": "SERPER_API_KEY is not configured"}
      │
      ├─ httpx.Client.post("https://google.serper.dev/search")
      │    └─ headers: {"X-API-KEY": api_key}
      │    └─ payload: {"q": query, "num": max_results}
      │
      ├─ response.json()["organic"]
      │    └─ Google 搜索的自然结果
      │
      └─ 标准化 → JSON
           [{"title": ..., "url": ..., "content": r.get("snippet")}]
```

**错误处理**：HTTP 错误返回 `{"error": "Serper API error: HTTP xxx"}`，网络异常返回 `{"error": str(e)}`。Agent 根据是否有 error 字段判断成功/失败。

### 4.3 DDG 流程（零配置）

```
web_search_tool(query)
      │
      ├─ from ddgs import DDGS
      │    └─ ImportError → 返回空结果
      │
      ├─ DDGS(timeout=30).text(query, region="wt-wt", max_results=5)
      │
      └─ 标准化 → JSON
           [{"title": ..., "url": r.get("href"), "content": r.get("body")}]
```

**为什么有 region 参数**：DuckDuckGo 支持区域化搜索（`"wt-wt"` 全球、`"cn-zh"` 中国等），默认全球搜索。

### 4.4 Exa 流程

```
web_search_tool(query)
      │
      ├─ Exa(api_key=config.api_key)
      │
      ├─ client.search(query, type="auto", num_results=5,
      │                contents={"highlights": {"max_characters": 1000}})
      │
      └─ 标准化 → JSON
           [{"title": ..., "url": ..., "snippet": "\n".join(result.highlights)}]
```

**高亮机制**：Exa 返回的 `highlights` 是内容摘要列表，用换行符连接为 snippet。`contents_max_characters` 可配置高亮长度。

---

## 五、网页抓取实现详解

### 5.1 Jina 流程（异步 + Readability）

```
web_fetch_tool(url)                       ← async def
      │
      ├─ JinaClient()
      │
      ├─ await jina_client.crawl(url, return_format="html", timeout=10)
      │    └─ httpx.AsyncClient.post("https://r.jina.ai/")
      │    └─ headers: {"X-Return-Format": "html", "Authorization": "Bearer ..."}
      │
      ├─ html_content.startswith("Error:") → 直接返回错误
      │
      ├─ await asyncio.to_thread(readability_extractor.extract_article, html_content)
      │    └─ 在线程池中执行 CPU 密集的 HTML 解析
      │    └─ 使用 Mozilla Readability 算法提取正文
      │
      └─ article.to_markdown()[:4096]
```

**为什么 Jina 需要 Readability 而 Firecrawl 不需要**：Jina Client 请求 `return_format="html"`（原始 HTML），需要客户端提取正文。Firecrawl 的 `scrape(formats=["markdown"])` 在服务端完成正文提取，直接返回干净的 Markdown。

### 5.2 Firecrawl 流程（直接 Markdown）

```
web_fetch_tool(url)
      │
      ├─ FirecrawlApp(api_key=config.api_key)
      │
      ├─ client.scrape(url, formats=["markdown"])
      │    └─ 服务端完成 HTML → Markdown 转换
      │    └─ 返回 result.markdown + result.metadata.title
      │
      └─ f"# {title}\n\n{markdown_content[:4096]}"
```

**getattr 安全访问**：Firecrawl 的 `SearchResultWeb` 对象使用属性（而非字典），`getattr(item, "title", "")` 避免 `AttributeError`。

### 5.3 InfoQuest 流程（三工具共享）

```
_get_infoquest_client()
      │
      ├─ 读取 web_search 配置 → search_time_range
      ├─ 读取 web_fetch 配置 → fetch_time, timeout, navigation_timeout
      ├─ 读取 image_search 配置 → image_search_time_range, image_size
      │
      └─ InfoQuestClient(search_time_range=..., ...)

web_search_tool(query) → client.web_search(query)
web_fetch_tool(url)
      ├─ client.fetch(url) → HTML
      ├─ readability_extractor.extract_article(html) → Markdown
      └─ [:4096]

image_search_tool(query) → client.image_search(query)
```

**InfoQuest 的结果清洗**：`clean_results()` 处理两层结构——`content_list["content"]["results"]` 包含 `organic`（网页）和 `top_stories`（新闻），分别提取并去重。

---

## 六、错误处理统一模式

所有工具遵循相同的错误处理约定：

| 场景 | 返回格式 | Agent 感知方式 |
|------|---------|---------------|
| API Key 未配置 | `{"error": "...KEY is not configured"}` | JSON 含 error 键 |
| HTTP 错误 | `{"error": "API error: HTTP xxx"}` | JSON 含 error 键 |
| 网络异常 | `{"error": str(e)}` | JSON 含 error 键 |
| 无结果 | `{"error": "No results found"}` | JSON 含 error 键 |
| 抓取失败 | `"Error: ..."` | 字符串以 Error: 开头 |

工具**永远不抛异常**——所有错误被 try/catch 捕获，转换为结构化错误信息返回给 Agent。这确保 Agent 不会因外部 API 故障而中断运行。

---

## 七、文件职责速查表

| 文件 | 代码行 | 核心工具 | 依赖库 |
|------|--------|---------|--------|
| `tavily/tools.py` | ~113 | web_search, web_fetch | tavily-python |
| `serper/tools.py` | ~146 | web_search | httpx |
| `ddg_search/tools.py` | ~136 | web_search | ddgs |
| `firecrawl/tools.py` | ~136 | web_search, web_fetch | firecrawl-py |
| `exa/tools.py` | ~144 | web_search, web_fetch | exa-py |
| `jina_ai/tools.py` | ~77 | web_fetch | httpx + readability |
| `jina_ai/jina_client.py` | ~105 | HTTP 客户端 | httpx |
| `infoquest/tools.py` | ~162 | web_search, web_fetch, image_search | requests + readability |
| `infoquest/infoquest_client.py` | ~587 | API 客户端 | requests |
| `image_search/tools.py` | ~181 | image_search | ddgs |
