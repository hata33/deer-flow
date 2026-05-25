# 搜索工具详解

> 源码路径：`backend/packages/harness/deerflow/community/`

DeerFlow 提供 4 种网页搜索工具实现，均注册为 LangChain `@tool("web_search")`。用户可根据需求选择一种，在 `config.yaml` 中配置即可切换。

---

## 1. Tavily 搜索

> 源码：`community/tavily/tools.py`

### 概述

Tavily 是专为 AI Agent 设计的搜索 API，提供高质量结构化搜索结果。DeerFlow 的 Tavily 集成包含两个工具：`web_search_tool` 和 `web_fetch_tool`。

### 工具列表

| 工具名 | LangChain 注册名 | 功能 |
|:-------|:------------------|:-----|
| `web_search_tool` | `web_search` | 网页搜索 |
| `web_fetch_tool` | `web_fetch` | 网页内容提取 |

### web_search_tool

```python
@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web."""
```

**参数**：

| 参数 | 类型 | 说明 |
|:-----|:-----|:-----|
| `query` | `str` | 搜索关键词 |

**配置项**（通过 `config.yaml` 的 `web_search` 节）：

| 配置键 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `api_key` | 必填 | Tavily API Key（支持 `$ENV_VAR` 引用） |
| `max_results` | `5` | 最大返回结果数 |

**返回格式**：JSON 数组，每个元素包含：

```json
[
  {
    "title": "页面标题",
    "url": "https://example.com",
    "snippet": "搜索结果摘要"
  }
]
```

**内部流程**：
1. `_get_tavily_client()` 从配置中获取 API Key，创建 `TavilyClient` 实例
2. 调用 `client.search(query, max_results=max_results)` 执行搜索
3. 将原始结果标准化为 `{title, url, snippet}` 格式
4. 通过 `json.dumps()` 序列化返回

### web_fetch_tool

```python
@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL."""
```

**参数**：

| 参数 | 类型 | 说明 |
|:-----|:-----|:-----|
| `url` | `str` | 目标网页 URL（必须包含 schema） |

**内部流程**：
1. 调用 `client.extract([url])` 提取网页内容
2. 检查 `failed_results` 和 `results` 两个分支
3. 成功时返回 `# {title}\n\n{raw_content[:4096]}`，内容截断到 4KB

**内容长度限制**：`raw_content` 截断到 **4096 字符**。

### 配置示例

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.tavily.tools:web_search_tool
  - name: web_fetch
    use: deerflow.community.tavily.tools:web_fetch_tool

web_search:
  api_key: $TAVILY_API_KEY
  max_results: 10
```

### 依赖

```
pip install tavily-python
```

---

## 2. DuckDuckGo 搜索（DDG）

> 源码：`community/ddg_search/tools.py`

### 概述

DuckDuckGo 搜索是唯一的**免费、无需 API Key** 的搜索工具，适合快速部署和开发测试。使用 `ddgs` 库的 `DDGS` 客户端。

### 工具列表

| 工具名 | LangChain 注册名 | 功能 |
|:-------|:------------------|:-----|
| `web_search_tool` | `web_search` | 网页搜索 |

### web_search_tool

```python
@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information."""
```

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-------|:-----|
| `query` | `str` | — | 搜索关键词 |
| `max_results` | `int` | `5` | 最大返回结果数 |

**配置项**（通过 `config.yaml` 的 `web_search` 节）：

| 配置键 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `max_results` | `5` | 最大返回结果数（覆盖函数参数默认值） |

**返回格式**：JSON 对象，包含查询信息和结果列表：

```json
{
  "query": "搜索关键词",
  "total_results": 5,
  "results": [
    {
      "title": "页面标题",
      "url": "https://example.com",
      "content": "搜索结果摘要"
    }
  ]
}
```

**内部流程**：
1. `_search_text()` 内部 `try/except ImportError` 保护 `from ddgs import DDGS`
2. 创建 `DDGS(timeout=30)` 客户端
3. 调用 `ddgs.text(query, region, safesearch, max_results)` 执行搜索
4. 标准化结果为 `{title, url, content}` 格式（url 从 `href` 或 `link` 字段提取）
5. 无结果时返回 `{"error": "No results found", "query": ...}`

### 搜索参数

内部函数 `_search_text()` 支持以下可选参数（当前未暴露给 LLM，但可在代码中调整）：

| 参数 | 默认值 | 说明 |
|:-----|:-------|:-----|
| `region` | `"wt-wt"` | 搜索区域 |
| `safesearch` | `"moderate"` | 安全搜索级别 |

### 配置示例

```yaml
# config.yaml — 无需 API Key
tools:
  - name: web_search
    use: deerflow.community.ddg_search.tools:web_search_tool

web_search:
  max_results: 8
```

### 依赖

```
pip install ddgs
```

### 注意事项

- DuckDuckGo 搜索有频率限制，高频使用可能触发反爬机制
- 搜索质量可能不如商业 API（Tavily、Serper）
- 适合开发和低频使用场景

---

## 3. Serper 搜索（Google SERP）

> 源码：`community/serper/tools.py`

### 概述

Serper 是 Google SERP（Search Engine Results Page）API 的封装，提供实时 Google 搜索结果。需要 API Key，注册地址：https://serper.dev。

### 工具列表

| 工具名 | LangChain 注册名 | 功能 |
|:-------|:------------------|:-----|
| `web_search_tool` | `web_search` | Google 网页搜索 |

### web_search_tool

```python
@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = 5) -> str:
    """Search the web for information using Google Search via Serper."""
```

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-------|:-----|
| `query` | `str` | — | 搜索关键词 |
| `max_results` | `int` | `5` | 最大返回结果数 |

**API Key 获取**（优先级从高到低）：

1. `config.yaml` 中 `web_search.api_key` 配置项
2. 环境变量 `SERPER_API_KEY`

**返回格式**：JSON 对象：

```json
{
  "query": "搜索关键词",
  "total_results": 5,
  "results": [
    {
      "title": "页面标题",
      "url": "https://example.com",
      "content": "搜索结果摘要"
    }
  ]
}
```

**错误处理**：

| 场景 | 返回值 |
|:-----|:-------|
| API Key 未配置 | `{"error": "SERPER_API_KEY is not configured", "query": ...}` |
| HTTP 错误 | `{"error": "Serper API error: HTTP {status_code}", "query": ...}` |
| 网络异常 | `{"error": "{exception_message}", "query": ...}` |
| 无结果 | `{"error": "No results found", "query": ...}` |

**内部流程**：
1. `_get_api_key()` 从配置或环境变量获取 API Key
2. 使用 `httpx.Client(timeout=30)` 向 `https://google.serper.dev/search` 发送 POST 请求
3. 请求头包含 `X-API-KEY`，请求体为 `{"q": query, "num": max_results}`
4. 解析响应中的 `organic` 字段，标准化为 `{title, url, content}` 格式

**API Key 缺失警告**：使用模块级 `_api_key_warned` 标志，仅首次缺失时打印警告日志，避免重复日志污染。

### 配置示例

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.serper.tools:web_search_tool

web_search:
  api_key: $SERPER_API_KEY
  max_results: 10
```

### 依赖

```
pip install httpx
```

---

## 4. Exa 搜索

> 源码：`community/exa/tools.py`

### 概述

Exa 是专为 AI 优化的搜索引擎，支持语义搜索和高质量内容提取。DeerFlow 的 Exa 集成包含搜索和内容提取两个工具。

### 工具列表

| 工具名 | LangChain 注册名 | 功能 |
|:-------|:------------------|:-----|
| `web_search_tool` | `web_search` | AI 优化搜索 |
| `web_fetch_tool` | `web_fetch` | 网页内容提取 |

### web_search_tool

```python
@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web."""
```

**参数**：

| 参数 | 类型 | 说明 |
|:-----|:-----|:-----|
| `query` | `str` | 搜索关键词 |

**配置项**：

| 配置键 | 默认值 | 说明 |
|:-------|:-------|:-----|
| `api_key` | 必填 | Exa API Key |
| `max_results` | `5` | 最大返回结果数 |
| `search_type` | `"auto"` | 搜索类型（auto/keyword/neural） |
| `contents_max_characters` | `1000` | highlights 内容最大字符数 |

**返回格式**：JSON 数组：

```json
[
  {
    "title": "页面标题",
    "url": "https://example.com",
    "snippet": "高亮摘要（多行合并）"
  }
]
```

**内部流程**：
1. `_get_exa_client()` 从配置中获取 API Key，创建 `Exa` 实例
2. 调用 `client.search(query, type=search_type, num_results=max_results, contents={...})`
3. 从 `result.highlights`（列表）合并为 `snippet` 字符串
4. 错误时返回 `"Error: {message}"`

### web_fetch_tool

```python
@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL."""
```

**参数**：

| 参数 | 类型 | 说明 |
|:-----|:-----|:-----|
| `url` | `str` | 目标网页 URL |

**内部流程**：
1. `_get_exa_client("web_fetch")` 从 `web_fetch` 配置节获取 API Key
2. 调用 `client.get_contents([url], text={"max_characters": 4096})`
3. 成功时返回 `# {title}\n\n{text[:4096]}`
4. 无结果时返回 `"Error: No results found"`

### 配置示例

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.exa.tools:web_search_tool
  - name: web_fetch
    use: deerflow.community.exa.tools:web_fetch_tool

web_search:
  api_key: $EXA_API_KEY
  max_results: 10
  search_type: neural
  contents_max_characters: 2000
web_fetch:
  api_key: $EXA_API_KEY
```

### 依赖

```
pip install exa-py
```

---

## 工具对比总览

| 特性 | Tavily | DuckDuckGo | Serper | Exa |
|:-----|:-------|:-----------|:-------|:----|
| **API Key** | 必须 | 不需要 | 必须 | 必须 |
| **搜索质量** | 高 | 中 | 高（Google） | 高（AI 优化） |
| **语义搜索** | 是 | 否 | 否 | 是（neural） |
| **网页提取** | 是（extract） | 否 | 否 | 是（get_contents） |
| **频率限制** | 按 Plan | 有 | 按 Plan | 按 Plan |
| **成本** | 免费/付费 | 免费 | 免费/付费 | 免费/付费 |
| **推荐场景** | 生产环境 | 开发/测试 | 需要 Google 结果 | AI/语义搜索 |

## 错误处理统一模式

所有搜索工具遵循一致的错误处理模式：

1. **返回 JSON 错误**：不抛出异常，而是返回包含 `error` 字段的 JSON 字符串
2. **保留查询上下**：错误响应中包含 `query` 字段，便于 Agent 理解失败上下文
3. **日志记录**：所有错误通过 `logger.error()` 记录完整异常信息
4. **优雅降级**：API Key 缺失时返回提示性错误消息，而非崩溃

## 返回格式标准化

所有搜索工具的输出均经过标准化处理，统一为以下结构：

```json
{
  "title": "页面标题",
  "url": "页面链接",
  "snippet" | "content": "内容摘要"
}
```

不同工具的原始字段映射：

| 工具 | title 来源 | url 来源 | snippet 来源 |
|:-----|:----------|:---------|:------------|
| Tavily | `result["title"]` | `result["url"]` | `result["content"]` |
| DDG | `r.get("title")` | `r.get("href")` | `r.get("body")` |
| Serper | `r.get("title")` | `r.get("link")` | `r.get("snippet")` |
| Exa | `result.title` | `result.url` | `result.highlights` |
