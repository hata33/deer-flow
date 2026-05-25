# 社区工具模块全局概览

> 源码路径：`backend/packages/harness/deerflow/community/`

## 模块定位

`community/` 是 DeerFlow 的**可选社区工具集**，提供搜索引擎集成、网页抓取、Docker 沙箱、图像搜索等扩展能力。所有工具均为**可选依赖**——系统在未安装对应包时会优雅降级，不影响核心功能运行。

社区工具通过 `config.yaml` 中的 `tools` 配置项按需加载，由反射模块 `deerflow.reflection.resolve_variable` 在运行时将字符串路径解析为实际的 LangChain `BaseTool` 实例。

## 模块结构

```
community/
├── aio_sandbox/              # Docker 容器沙箱（6 个文件）
│   ├── __init__.py           # 导出 AioSandbox, AioSandboxProvider 等
│   ├── aio_sandbox.py        # AioSandbox 沙箱实现
│   ├── aio_sandbox_provider.py  # AioSandboxProvider 生命周期管理
│   ├── backend.py            # SandboxBackend 抽象基类
│   ├── local_backend.py      # 本地 Docker/Apple Container 后端
│   ├── remote_backend.py     # 远程 K8s Provisioner 后端
│   └── sandbox_info.py       # SandboxInfo 元数据
├── tavily/                   # Tavily 搜索（1 个文件）
│   └── tools.py              # web_search + web_fetch 工具
├── ddg_search/               # DuckDuckGo 搜索（2 个文件）
│   ├── __init__.py
│   └── tools.py              # web_search 工具
├── serper/                   # Serper Google 搜索（2 个文件）
│   ├── __init__.py
│   └── tools.py              # web_search 工具
├── exa/                      # Exa AI 搜索（1 个文件）
│   └── tools.py              # web_search + web_fetch 工具
├── jina_ai/                  # Jina AI 网页抓取（2 个文件）
│   ├── jina_client.py        # Jina API 客户端
│   └── tools.py              # web_fetch 工具
├── firecrawl/                # Firecrawl 网页抓取（1 个文件）
│   └── tools.py              # web_search + web_fetch 工具
├── image_search/             # DuckDuckGo 图像搜索（2 个文件）
│   ├── __init__.py
│   └── tools.py              # image_search 工具
└── infoquest/                # BytePlus InfoQuest（2 个文件）
    ├── infoquest_client.py   # InfoQuest API 客户端
    └── tools.py              # web_search + web_fetch + image_search
```

共 **20 个源文件**，分布在 **9 个子目录**中。

## 模块分类

| 分类 | 子目录 | 核心能力 | 需要 API Key |
|------|--------|----------|:------------:|
| **搜索工具** | `tavily/` | Tavily 搜索 + 网页提取 | 是 |
| | `ddg_search/` | DuckDuckGo 免费搜索 | 否 |
| | `serper/` | Google SERP 搜索 | 是 |
| | `exa/` | AI 优化搜索引擎 | 是 |
| **网页工具** | `jina_ai/` | Jina Reader 网页提取 | 可选 |
| | `firecrawl/` | Firecrawl 结构化抓取 | 是 |
| | `infoquest/` | BytePlus 搜索 + 抓取 + 图像搜索 | 可选 |
| **图像搜索** | `image_search/` | DuckDuckGo 图像搜索 | 否 |
| **沙箱** | `aio_sandbox/` | Docker 容器隔离执行 | 否 |

## 可选依赖设计

社区工具遵循**优雅降级**原则：

1. **工具注册阶段**：`config.yaml` 中配置的工具通过 `resolve_variable(cfg.use, BaseTool)` 加载。如果目标模块的依赖包未安装，`importlib.import_module` 会抛出 `ImportError`，系统记录警告日志并跳过该工具。

2. **运行时依赖**：部分工具在内部使用 `try/except ImportError` 保护第三方库导入。例如 `ddg_search/tools.py` 中：
   ```python
   try:
       from ddgs import DDGS
   except ImportError:
       logger.error("ddgs library not installed. Run: pip install ddgs")
       return []
   ```

3. **API Key 检测**：需要 API Key 的工具在缺少配置时返回友好错误信息，而非抛出异常。例如 `serper/tools.py` 返回 `{"error": "SERPER_API_KEY is not configured"}`。

## 工具注册机制

社区工具的注册流程与核心工具完全一致：

```
config.yaml                  reflection                   tools/tools.py
───────────                  ──────────                   ───────────────
tools:                                                       
  - name: web_search        resolve_variable()           
    use: deerflow.community ──────────────────────────►  get_available_tools()
              .tavily.tools      │                         
              :web_search_tool   ▼                        
                            导入模块并获取变量            
```

1. **配置声明**：在 `config.yaml` 的 `tools` 列表中声明工具的 `use` 路径
2. **路径解析**：`resolve_variable("deerflow.community.tavily.tools:web_search_tool", BaseTool)` 将字符串拆分为模块路径和变量名
3. **动态导入**：`importlib.import_module("deerflow.community.tavily.tools")` 加载模块
4. **实例获取**：从模块中获取 `web_search_tool` 变量，验证其为 `BaseTool` 实例
5. **工具装配**：`get_available_tools()` 按优先级合并 config 工具、内置工具、MCP 工具

## 工具名称约定

社区工具注册到 LangChain 时使用标准化的工具名称：

| LangChain 工具名 | 提供者 | 说明 |
|:-----------------|:-------|:-----|
| `web_search` | tavily, ddg, serper, exa, firecrawl, infoquest | 网页搜索 |
| `web_fetch` | tavily, exa, firecrawl, jina_ai, infoquest | 网页内容提取 |
| `image_search` | image_search, infoquest | 图像搜索 |

多个社区工具可以注册同名工具（如 `web_search`），但 `get_available_tools()` 会按优先级去重——config.yaml 中先声明的工具优先。

## 依赖包汇总

| 工具 | Python 包 | 安装命令 |
|:-----|:----------|:---------|
| Tavily | `tavily-python` | `pip install tavily-python` |
| DuckDuckGo | `ddgs` | `pip install ddgs` |
| Serper | `httpx` | `pip install httpx` |
| Exa | `exa-py` | `pip install exa-py` |
| Jina AI | `httpx` | `pip install httpx` |
| Firecrawl | `firecrawl-py` | `pip install firecrawl-py` |
| Image Search | `ddgs` | `pip install ddgs` |
| InfoQuest | `requests` | `pip install requests` |
| AIO Sandbox | `agent-sandbox` | `pip install agent-sandbox` |

所有依赖包均未在 `pyproject.toml` 中声明为必需依赖。用户按需安装，系统自动检测。

## 配置示例

```yaml
# config.yaml
tools:
  - name: web_search
    use: deerflow.community.tavily.tools:web_search_tool
    group: search
  - name: web_fetch
    use: deerflow.community.jina_ai.tools:web_fetch_tool
    group: search
  - name: image_search
    use: deerflow.community.image_search.tools:image_search_tool
    group: search

# 搜索工具的 API Key 和参数配置
web_search:
  api_key: $TAVILY_API_KEY
  max_results: 5
web_fetch:
  timeout: 15
```

## 相关文档

- [01-search-tools.md](./01-search-tools.md) — 搜索工具详解
- [02-web-tools.md](./02-web-tools.md) — 网页工具详解
- [03-aio-sandbox.md](./03-aio-sandbox.md) — AIO Docker 沙箱详解
- [04-lifecycle.md](./04-lifecycle.md) — 工具生命周期流程
