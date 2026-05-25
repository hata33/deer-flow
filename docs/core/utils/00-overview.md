# 工具函数模块 — 全局概览

## 定位

DeerFlow 工具函数模块（`deerflow.utils`）提供一组独立的通用工具函数，涵盖文档转换、网络端口分配、网页内容提取和 ISO 8601 时间戳处理。每个子模块解决一个具体的基础设施问题，无跨模块依赖，可独立使用。

> **关键边界**：工具函数模块是"纯工具层"，不包含业务逻辑，不依赖 FastAPI 或其他 DeerFlow 业务模块。

## 源文件

```
backend/packages/harness/deerflow/utils/
├── file_conversion.py    # 文档转 Markdown（PDF 双转换器策略、大纲提取）
├── network.py            # 线程安全端口分配
├── readability.py        # 网页内容提取（HTML → Article → Markdown）
└── time.py               # ISO 8601 时间戳生成和兼容性转换
```

## 模块总览

| 模块 | 核心类/函数 | 使用场景 |
|------|-------------|----------|
| `file_conversion` | `convert_file_to_markdown()`, `extract_outline()` | 上传文件自动转换、Agent 上下文注入 |
| `network` | `PortAllocator`, `get_free_port()`, `release_port()` | Docker 沙箱端口映射、本地服务端口分配 |
| `readability` | `ReadabilityExtractor`, `Article` | 网页搜索工具的内容提取、HTML 清洗 |
| `time` | `now_iso()`, `coerce_iso()` | 所有时间戳的标准化生成、历史数据兼容 |

---

## 1. file_conversion — 文档转换系统

### 定位

将 PDF、PPT、Excel、Word 等文档文件转换为 Markdown 文本，使 Agent 能够阅读和处理上传的文档内容。支持大纲提取用于长文档的上下文注入。

### PDF 双转换器策略

PDF 转换采用双转换器策略，根据配置和输出质量自动选择最优路径：

```
                    ┌─────────────────────┐
                    │  pdf_converter 配置  │
                    └─────────┬───────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
          "markitdown"     "auto"       "pymupdf4llm"
              │               │               │
              ▼               ▼               ▼
     直接使用          pymupdf4llm 优先     强制 pymupdf4llm
     MarkItDown            │              （不管输出长度）
                          │
                    ┌─────┴─────┐
                    │           │
              输出足够长     输出过于稀疏
              (>50 字符/页)  (<=50 字符/页)
                    │           │
                    ▼           ▼
              使用 pymupdf   回退到 MarkItDown
              结果           （可能是图片 PDF）
```

**为什么需要双转换器**：

| 特性 | pymupdf4llm | MarkItDown |
|------|-------------|------------|
| 标题检测 | 更好的标题层级识别 | 基础标题识别 |
| 速度 | 大多数文件更快 | 较慢但更通用 |
| 图片 PDF | 输出几乎为空（需回退） | 可通过 OCR 处理 |
| 安装 | 可选依赖（`pymupdf4llm`） | 核心依赖（`markitdown`） |

### 核心常量

| 常量 | 值 | 说明 |
|------|----|------|
| `CONVERTIBLE_EXTENSIONS` | `{".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".doc", ".docx"}` | 支持自动转换的文件扩展名 |
| `_ASYNC_THRESHOLD_BYTES` | `1 MB` | 超过此阈值在后台线程转换 |
| `_MIN_CHARS_PER_PAGE` | `50` | pymupdf 输出低于此值视为图片 PDF |
| `MAX_OUTLINE_ENTRIES` | `50` | 大纲最多提取条目数（控制 prompt 大小） |

### 核心函数

**`convert_file_to_markdown(file_path) -> Path | None`**

```python
async def convert_file_to_markdown(file_path: Path) -> Path | None:
    # 1. 读取 PDF 转换器配置（auto/pymupdf4llm/markitdown）
    # 2. 大文件（>1MB）通过 asyncio.to_thread() 在线程池转换
    # 3. 小文件同步转换（避免线程调度开销）
    # 4. 写入 .md 文件并返回路径
```

**`extract_outline(md_path) -> list[dict]`**

从 Markdown 文件提取文档大纲（标题列表）。识别三种 pymupdf4llm 生成的标题风格：

| 风格 | 示例 | 正则匹配 |
|------|------|----------|
| 标准 Markdown 标题 | `## 1. Introduction` | 以 `#` 开头 |
| 粗体结构标题（SEC 文件） | `**ITEM 1. BUSINESS**` | `_BOLD_HEADING_RE` |
| 拆分粗体标题（学术论文） | `**3.2** **Multi-Head Attention**` | `_SPLIT_BOLD_HEADING_RE` |

返回格式：

```python
[
    {"title": "1. Introduction", "line": 12},
    {"title": "3.2 Multi-Head Attention", "line": 45},
    {"truncated": True}  # 超过 MAX_OUTLINE_ENTRIES 时追加的哨兵条目
]
```

### 大文件异步处理

超过 `_ASYNC_THRESHOLD_BYTES`（1 MB）的文件通过 `asyncio.to_thread()` 在线程池中转换，避免阻塞事件循环。小文件同步完成（通常 < 1 秒），无需线程调度开销。

### 配置来源

PDF 转换器策略通过 `config.yaml` 配置：

```yaml
uploads:
  pdf_converter: "auto"    # "auto" | "pymupdf4llm" | "markitdown"
```

无效值自动回退为 `"auto"` 并记录警告日志。

---

## 2. network — 线程安全端口分配

### 定位

提供线程安全的端口分配机制，防止并发环境中的端口冲突。主要用于 Docker 沙箱的端口映射和本地测试服务的端口分配。

### 核心类：PortAllocator

```python
class PortAllocator:
    def __init__(self):
        self._lock = threading.Lock()          # 互斥锁
        self._reserved_ports: set[int] = set() # 已分配端口集合
```

**分配策略**：

1. 获取线程锁
2. 从 `start_port` 开始逐个检查端口可用性
3. 可用性检查：先查 `_reserved_ports` 集合（O(1)），再尝试 `socket.bind(("0.0.0.0", port))`
4. 找到可用端口后加入 `_reserved_ports` 并返回
5. 在 `max_range` 范围内找不到可用端口则抛出 `RuntimeError`

**绑定地址选择**：使用 `0.0.0.0`（通配符）而非 `127.0.0.1`，与 Docker 绑定行为一致。仅检查 localhost 可能误报端口可用（Docker 已在通配符地址占用）。

### 使用方式

```python
# 方式 1：手动分配和释放
port = allocator.allocate(start_port=8080)
try:
    # 使用端口...
finally:
    allocator.release(port)

# 方式 2：上下文管理器（推荐）
with allocator.allocate_context(start_port=8080) as port:
    # 使用端口...
    pass  # 自动释放
```

### 全局分配器实例

模块提供全局单例和便捷函数：

```python
# 全局分配器
_global_port_allocator = PortAllocator()

# 便捷函数
get_free_port(start_port=8080, max_range=100) -> int
release_port(port: int) -> None
```

全局分配器确保跨模块的端口分配不会冲突。

---

## 3. readability — 网页内容提取

### 定位

从 HTML 页面中提取正文内容和标题，将杂乱的网页 HTML 转换为结构化的 `Article` 对象，最终输出为干净的 Markdown 文本或 LLM 可消费的消息格式。

### 核心类

**ReadabilityExtractor**：

```python
class ReadabilityExtractor:
    def extract_article(self, html: str) -> Article:
        # 优先使用 Readability.js（通过 Node.js 子进程）
        # 失败时回退到纯 Python 提取
```

提取策略：

| 策略 | 依赖 | 质量 | 回退条件 |
|------|------|------|----------|
| Readability.js | Node.js + `readability` 包 | 高（Mozilla 算法） | `CalledProcessError` / `FileNotFoundError` |
| 纯 Python | `readabilipy` 内置 | 中 | — （最终回退） |

**Article**：

```python
class Article:
    title: str           # 文章标题
    html_content: str    # 清洗后的 HTML 正文
    url: str             # 原始 URL（用于解析相对路径图片）

    def to_markdown(including_title=True) -> str:
        # HTML → Markdown 转换（使用 markdownify）

    def to_message() -> list[dict]:
        # 拆分为文本块和图片块的多模态消息格式
```

**`to_message()` 输出格式**：

```python
[
    {"type": "text", "text": "# Article Title\n\nArticle body text..."},
    {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
    {"type": "text", "text": "More text after the image..."},
]
```

图片 URL 通过 `urljoin(self.url, ...)` 解析为绝对路径，确保相对路径图片可正确引用。

### 使用场景

- 网页搜索工具（Tavily、Jina AI、Firecrawl）获取到 HTML 后的内容提取
- Agent 需要阅读网页正文时，将 HTML 清洗为 Markdown
- 多模态 LLM 场景中，提取文章文字和图片分别传入

---

## 4. time — ISO 8601 时间戳

### 定位

提供统一的时间戳生成和历史数据兼容性转换，确保 DeerFlow 所有组件（Gateway、RunManager、Checkpoint）使用一致的 ISO 8601 UTC 时间格式，匹配 LangGraph Platform 的 schema 约定。

### 核心函数

**`now_iso() -> str`**

生成当前 UTC 时间的 ISO 8601 字符串：

```python
>>> now_iso()
"2026-04-27T03:19:46.511479+00:00"
```

- 使用 `datetime.now(UTC).isoformat()` 生成
- 带有完整时区偏移（`+00:00`），不使用 `Z` 后缀
- 微秒精度保留

**`coerce_iso(value) -> str`**

将各种历史格式统一转换为 ISO 8601 字符串：

| 输入类型 | 示例 | 输出 |
|----------|------|------|
| `str`（ISO 格式） | `"2026-04-27T03:19:46+00:00"` | 原样返回 |
| `str`（Unix 时间戳） | `"1714187986.511479"` | 转换为 ISO |
| `int` / `float` | `1714187986` | 转换为 ISO |
| `datetime`（有 tz） | `datetime(2026, 4, 27, tzinfo=UTC)` | `isoformat()` |
| `datetime`（无 tz） | `datetime(2026, 4, 27)` | 视为 UTC，附加 `+00:00` |
| `None` / `""` | — | `""` |
| `bool` | `True` | `"True"`（视为垃圾数据，不转为时间戳） |

### Unix 时间戳识别

```python
_UNIX_TIMESTAMP_PATTERN = re.compile(r"^\d{10}(?:\.\d+)?$")
```

仅匹配 10 位数字（可选小数部分）的字符串，避免误将 ISO 年份（如 `"2026"`）当作 Unix 时间戳。10 位 Unix 时间戳有效期至 2286 年。

### 设计决策

- **`bool` 特殊处理**：`bool` 是 `int` 的子类，不处理的话 `True` 会被转为 `1970-01-01T00:00:01+00:00`，因此优先判断并返回 `str(value)`
- **`datetime` 优先于 `int`**：`datetime` 也是对象，必须在 `int`/`float` 判断之前处理，否则 `str(datetime)` 会使用空格分隔符（不符合 ISO 8601 严格格式）
- **历史兼容无需迁移**：`coerce_iso()` 提供了向前兼容的读取路径，旧版 DeerFlow 存储的 `str(time.time())` 浮点数字符串会在读取时自动转换，无需一次性数据迁移

---

## 跨模块关系

```
上传流程中的工具函数协作：

用户上传文件（Gateway API / DeerFlowClient）
    │
    ├── uploads.manager — 文件安全存储
    │
    └── utils.file_conversion — 文档自动转换
            │
            └── utils.readability — （间接：网页工具使用）

Agent 运行时的工具函数使用：

    ├── utils.time — now_iso() 生成所有时间戳
    │
    ├── utils.network — 沙箱端口分配
    │
    └── utils.readability — 网页搜索结果提取
```
