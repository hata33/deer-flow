# 05 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **@tool 装饰器模式（LangChain 标准）** | 自动生成 JSON Schema，与 Agent 工具系统无缝集成 |
| 2 | **标准化输出格式 {title, url, snippet/content}** | 跨搜索引擎和抓取工具的统一接口 |
| 3 | **web_fetch 截断 4096 字符** | 平衡信息完整性与 LLM 上下文预算 |
| 4 | **双引擎 PDF 转换** | pymupdf4llm 质量优先 + markitdown 兼容回退 |
| 5 | **API Key 配置优先，环境变量回退** | 灵活性——配置文件和环境变量两种方式都支持 |
| 6 | **Jina 异步 + Readability 提取** | 适配异步 Agent 框架 + 去噪得到正文 |
| 7 | **InfoQuest 三工具共享单客户端** | 统一认证/配置，减少重复代码 |

---

## 二、逐决策分析

### 决策 1：@tool 装饰器模式

**问题**：如何将社区工具注册到 LangChain Agent 系统？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 手动构建 BaseTool 子类 | 完全控制 | 代码量大，需手写 schema |
| @tool 装饰器（当前） | 自动从 docstring 生成 schema，零样板代码 | schema 精度受 docstring 格式影响 |
| StructuredTool | 灵活 | 需手动指定 args_schema |

**选择 @tool**：`@tool("web_search", parse_docstring=True)` 从函数签名和 Google-style docstring 自动提取参数描述，生成 OpenAI function calling 格式的 JSON Schema。Agent 框架直接消费这些 schema——无需额外适配。

**工具名约定**：不同搜索引擎模块都注册为 `web_search`，不同抓取模块都注册为 `web_fetch`。`config.yaml` 通过 `use` 字段决定加载哪个实现（如 `deerflow.community.tavily.tools:web_search_tool`），同一时间只有一个 `web_search` 工具生效。

---

### 决策 2：标准化输出格式

**问题**：不同搜索/抓取 API 返回格式各异，Agent 需要统一处理。

| API | 原始字段 | 标准化后 |
|-----|---------|----------|
| Tavily | title, url, content | {title, url, snippet} |
| Serper | title, link, snippet | {title, url, content} |
| DDG | title, href, body | {title, url, content} |
| Firecrawl | title, url, description | {title, url, snippet} |
| Exa | title, url, highlights[] | {title, url, snippet} |

**标准化映射**：每个工具在内部做字段重命名，输出 JSON 统一为 `{title, url, snippet/content}` 格式。Agent 的后续逻辑（引用 URL、展示标题）不依赖特定 API。

**为什么用 JSON 字符串而非结构化返回**：`@tool` 装饰器返回字符串，Agent 通过文本理解内容。JSON 格式保证结构化可解析，同时不影响 LLM 理解。

---

### 决策 3：web_fetch 截断 4096 字符

**问题**：网页内容可能非常长（数万字符），全部返回会占满 LLM 上下文窗口。

| 截断长度 | 信息量 | 上下文占用 |
|----------|--------|-----------|
| 1024 | 可能丢失关键信息 | 极低 |
| 4096（当前） | 平衡信息与成本 | 约 1000 tokens |
| 不截断 | 完整信息 | 可能超限 |

**选择 4096**：经过实践验证，4096 字符约对应 1000 tokens，足以覆盖大多数网页的核心内容（前几段 + 关键段落）。搜索场景下 Agent 通常只需要摘要和关键事实，不需要完整页面。

**为什么在工具层截断而非中间件层**：工具最了解内容的边界——Markdown 标题后的正文、代码块等，在工具层截断可以做到合理的断点（如不在 markdown 语法中间截断）。中间件无法做这种内容感知的截断。

---

### 决策 4：双引擎 PDF 转换（上传文件处理）

**问题**：用户上传 PDF 后需要转为文本供 Agent 理解，不同 PDF 质量差异大。

| 方案 | 优势 | 劣势 |
|------|------|------|
| markitdone（单一引擎） | 零依赖（Microsoft 库） | 复杂排版质量不稳定 |
| pymupdf4llm 优先 + markitdown 回退（当前） | 高质量转换 | 需安装两个依赖 |

**双引擎策略**：先尝试 pymupdf4llm（基于 MuPDF，对表格、多栏、公式等复杂排版处理更好）；如果失败或未安装，回退到 markitdown（Microsoft 的通用文档转换库，覆盖 PDF/PPT/Excel/Word）。

---

### 决策 5：API Key 配置优先

**问题**：API Key 存储在多个位置，如何确定优先级？

| 优先级 | 来源 | 场景 |
|--------|------|------|
| 1 | `config.yaml` 的 `tools.web_search.api_key` | 集中管理，多环境部署 |
| 2 | 环境变量（如 `TAVILY_API_KEY`） | 开发环境，CI/CD |

```python
# Serper 的典型实现
def _get_api_key() -> str | None:
    config = get_app_config().get_tool_config("web_search")
    if config is not None:
        api_key = config.model_extra.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            return api_key
    return os.getenv("SERPER_API_KEY")
```

**为什么配置优先**：`config.yaml` 支持版本控制和多环境配置，适合生产环境。环境变量适合开发者快速启动。两者都支持确保零配置也能用（如 DDG 不需要 Key）。

**缺失警告去重**：使用模块级 `_api_key_warned` 标志，确保缺失 Key 的警告只打印一次，避免日志噪音。

---

### 决策 6：Jina 异步 + Readability

**问题**：Jina 工具使用异步实现（`async def`），其他工具使用同步实现。为什么？

| 方面 | Jina（异步） | Tavily/Serper（同步） |
|------|-------------|---------------------|
| HTTP 客户端 | httpx.AsyncClient | httpx.Client / requests |
| 工具函数 | `async def` | `def` |
| Readability 提取 | `asyncio.to_thread()` | 直接调用 |

**为什么 Jina 用异步**：Jina 的 Reader API 延迟较高（需要服务端渲染页面），使用异步避免阻塞事件循环。`asyncio.to_thread()` 将 Readability 的 CPU 密集 HTML 解析卸载到线程池，不阻塞异步调度。

**Readability 的作用**：Jina Client 返回原始 HTML（包含导航栏、广告、页脚等噪音）。`ReadabilityExtractor` 使用 Mozilla 的 Readability 算法提取正文，再转为 Markdown——输出干净、信息密度高。

---

### 决策 7：InfoQuest 三工具共享单客户端

**问题**：InfoQuest 提供 web_search、web_fetch、image_search 三种工具，如何管理共享配置？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 三个独立客户端实例 | 完全隔离 | 重复认证/配置代码 |
| 共享单客户端实例（当前） | 统一认证和配置 | 略高的耦合 |

**选择共享**：`_get_infoquest_client()` 从三个配置段（web_search、web_fetch、image_search）读取各自参数，构建单个 `InfoQuestClient` 实例。API Key 统一从 `INFOQUEST_API_KEY` 环境变量获取，认证只需配置一次。

**结果清洗的差异化**：`clean_results()` 处理网页+新闻结果（URL 去重），`clean_results_with_image_search()` 处理图片结果（image_url 去重）。两者共享 `seen_urls` 去重逻辑，但输出格式不同。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **即插即用** | config.yaml 配置 `use` 字段即可切换搜索引擎/抓取工具 |
| **零成本入门** | DDG 无需 API Key，开箱即用 |
| **统一接口** | 所有工具输出 {title, url, snippet/content} 标准格式 |
| **上下文可控** | 4096 字符截断，约 1000 tokens |
| **错误隔离** | 工具异常返回 Error 字符串，不中断 Agent 运行 |
| **灵活认证** | 配置文件优先 + 环境变量回退 |
