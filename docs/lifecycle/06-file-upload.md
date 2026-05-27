# 文件上传全链路

> 从 HTTP 多部分上传到文档格式转换、线程存储、UploadsMiddleware 注入、沙箱挂载、记忆清洗的完整跨模块协作路径。

---

## 全链路架构图

```
┌──────────┐  multipart   ┌──────────┐  convert  ┌──────────────┐   store   ┌──────────────┐
│ Frontend │ ──────────▸  │ Gateway  │ ────────▸ │ File         │  ──────▸  │ Thread       │
│ (Drop    │   POST       │ Uploads  │  PDF/PPT/ │ Conversion   │           │ Directory    │
│  Zone)   │   /uploads   │ Router   │  Excel/   │ (markitdown) │           │ Structure    │
└──────────┘              └──────────┘  Word     └──────────────┘           └──────┬───────┘
                                                                                   │
       ┌───────────────────────────────────────────────────────────────────────────┤
       │                                                                           │
       ▼                                                                           ▼
┌──────────────┐  inject   ┌──────────────┐  mount   ┌──────────────┐  cleanup ┌──────────────┐
│ Uploads      │ ────────▸ │ Agent        │ ────────▸│ Sandbox      │ ────────▸│ Memory       │
│ Middleware   │  files    │ Conversation │  virtual │ Path Mapping │  upload  │ Updater      │
│              │  context  │              │  paths   │ /mnt/uploads │  mentions│ (strip refs) │
└──────────────┘           └──────────────┘          └──────────────┘          └──────────────┘
```

---

## 阶段 ①：HTTP 上传接收 — Gateway Upload Router

**核心文件**: `app/gateway/routers/uploads.py` → `upload_files()`

**入口端点**:
```
POST /api/threads/{thread_id}/uploads
Content-Type: multipart/form-data

files: [file1.pdf, file2.docx, ...]
```

**处理流程**:
1. 接收 multipart/form-data 上传请求
2. 调用 `normalize_filename()` 清洗文件名（防止路径穿越攻击）
3. 通过 `ensure_uploads_dir()` 确保线程上传目录存在
4. 使用 `_write_upload_file_with_limits()` 流式写入文件（带大小限制）
5. 检查 `_auto_convert_documents_enabled()` 是否启用自动转换
6. 如果启用，调用 `convert_file_to_markdown()` 转换文档
7. 将转换后的 Markdown 文件存放在原始文件旁边
8. 通过 `sandbox.update_file()` 同步文件到沙箱

**安全机制**:
- 文件名清洗：`normalize_filename()` 移除路径分隔符、特殊字符
- 拒绝目录路径输入（上传前检查）
- 同名文件自动重命名（`_N` 后缀）：同一上传请求中后出现的文件不会截断先前的文件
- 全有或全无策略：如果任一文件写入失败，整个上传回滚

**跨模块协作**:
- **Upload Router ↔ UploadsManager**: 文件存储操作委托
- **Upload Router ↔ FileConversion**: 文档格式转换
- **Upload Router ↔ Sandbox**: 文件同步到沙箱环境

---

## 阶段 ②：文档格式转换 — FileConversion

**核心文件**: `packages/harness/deerflow/utils/file_conversion.py`

**支持的转换格式**:

| 输入格式 | 转换引擎 | 输出 |
|---------|---------|------|
| PDF | pymupdf4llm → MarkItDown（回退） | Markdown |
| PPT (.pptx) | MarkItDown | Markdown |
| Excel (.xlsx) | MarkItDown | Markdown |
| Word (.docx) | MarkItDown | Markdown |

**双引擎 PDF 转换策略**:
```
PDF 文件
    │
    ├──▸ _convert_pdf_with_pymupdf4llm()    # 首选：质量更高
    │       │
    │       └──▸ 失败?
    │               │
    │               ▼
    │           _convert_with_markitdown()   # 回退：兼容性更好
    │
    └──▸ 结果: Markdown 文本
```

**转换输出**:
- 转换后的 `.md` 文件与原始文件存放在同一目录
- 同时生成文档大纲（`extract_outline()`）供后续注入使用

**事件循环处理**:
- 从活跃事件循环中调用时，复用单个 worker 执行转换
- 避免在异步上下文中阻塞事件循环

**跨模块协作**:
- **FileConversion ↔ Upload Router**: 被上传路由调用
- **FileConversion ↔ UploadsMiddleware**: 提供文档大纲数据

---

## 阶段 ③：线程存储与目录结构 — ThreadDataMiddleware

**核心文件**: `packages/harness/deerflow/agents/middlewares/thread_data_middleware.py`

**目录结构创建**:
```
backend/.deer-flow/users/{user_id}/threads/{thread_id}/
├── user-data/
│   ├── workspace/      # 工作空间（Agent 可读写）
│   ├── uploads/        # 上传文件存储
│   └── outputs/        # 输出文件（present_files 可见）
```

**用户隔离**:
- `user_id` 通过 `get_effective_user_id()` 从上下文获取
- 无认证模式下 `user_id` 默认为 `"default"`（`DEFAULT_USER_ID` 常量）
- 每个用户有独立的线程目录层级

**目录创建时机**:
- `ThreadDataMiddleware.before_agent()` 在每次代理执行前确保目录存在
- 幂等操作：目录已存在时不报错

**跨模块协作**:
- **ThreadDataMiddleware ↔ Upload Router**: 共享相同的目录路径约定
- **ThreadDataMiddleware ↔ SandboxProvider**: 沙箱路径映射基于此目录结构
- **ThreadDataMiddleware ↔ UserContext**: 通过 `get_effective_user_id()` 获取用户标识

---

## 阶段 ④：文件注入 — UploadsMiddleware

**核心文件**: `packages/harness/deerflow/agents/middlewares/uploads_middleware.py`

**拦截点**: `before_agent()`

**注入逻辑**:
```
用户上传了 report.pdf 和 data.xlsx

        ↓ UploadsMiddleware 注入

<uploaded_files>
- report.pdf (2.3 MB, uploaded 2026-05-26)
  Outline: 1. Introduction  2. Methods  3. Results...
  Preview: # Report\n\n## Introduction\nThis report...
- data.xlsx (156 KB, uploaded 2026-05-26)
  Preview: # Data\n\n| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |...
</uploaded_files>
```

**注入内容**:
1. **文件元信息**: 文件名、大小、上传时间
2. **文档大纲**: 通过 `_extract_outline_for_file()` 提取的结构化摘要
3. **内容预览**: 转换后 Markdown 的前若干字符

**数据来源**:
- 从消息的 `additional_kwargs.files` 读取上传文件列表
- 每个文件条目包含：原始路径、转换后的 Markdown 路径、元数据

**注入位置**:
- 作为第一条 HumanMessage 的前缀注入
- 不修改系统提示词（保持静态以利用 LLM 前缀缓存）

**跨模块协作**:
- **UploadsMiddleware ↔ ThreadDataMiddleware**: 依赖线程目录结构
- **UploadsMiddleware ↔ FileConversion**: 读取转换后的 Markdown 文件
- **UploadsMiddleware ↔ ThreadState**: 通过 `uploaded_files` 字段跟踪已上传文件

---

## 阶段 ⑤：沙箱挂载 — Sandbox Path Mapping

**核心文件**: `packages/harness/deerflow/sandbox/local/local_sandbox_provider.py` → `_build_thread_path_mappings()`

**虚拟路径映射**:
```
Agent 视角 (容器路径):              宿主实际路径:
/mnt/user-data/uploads/       →    {base}/users/{user_id}/threads/{tid}/user-data/uploads/
/mnt/user-data/workspace/     →    {base}/users/{user_id}/threads/{tid}/user-data/workspace/
/mnt/user-data/outputs/       →    {base}/users/{user_id}/threads/{tid}/user-data/outputs/
```

**挂载特性**:
- 每个 `LocalSandbox` 实例持有独立的 `PathMapping` 列表
- LRU 缓存（默认 256 条目）+ `threading.Lock` 保护
- 代理使用虚拟路径访问文件，沙箱负责翻译到宿主路径

**AIO Docker 模式**:
- 上传目录通过 Docker volume 挂载到容器内
- 容器内使用相同的虚拟路径（`/mnt/user-data/uploads/`）
- 两种实现（Local / AIO）共享相同的虚拟路径约定

**文件同步**:
- 上传路由通过 `sandbox.update_file()` 将文件推送到沙箱
- 在 Local 模式下，文件直接写入宿主目录，沙箱路径映射自动生效
- 在 AIO 模式下，需要显式同步到容器

**跨模块协作**:
- **Sandbox ↔ Upload Router**: 路由器调用 `sandbox.update_file()` 同步文件
- **Sandbox ↔ ThreadDataMiddleware**: 共享线程目录路径
- **Sandbox ↔ Config**: 读取沙箱配置（local / docker 模式）

---

## 阶段 ⑥：记忆清洗 — Memory Updater

**核心文件**: `packages/harness/deerflow/agents/memory/updater.py`

**清洗目的**:
上传文件引用是**会话级别**的，不应持久化到长期记忆中。当用户下次对话时，之前的上传文件可能已不存在。

**清洗机制**:
```python
_UPLOAD_SENTENCE_RE = re.compile(r"...")  # 匹配上传相关句子

def _strip_upload_mentions_from_memory(text: str) -> str:
    # 移除包含 <uploaded_files> 标签的内容
    # 移除匹配上传句子正则的文本
```

**清洗流程**:
1. `MemoryMiddleware` 在 `after_agent` 阶段收集对话消息
2. 消息被发送到 `MemoryQueue` 进行防抖处理
3. `MemoryUpdater` 调用 LLM 提取记忆更新
4. 在生成记忆更新前，调用 `_strip_upload_mentions_from_memory()` 清洗
5. 清洗后的记忆通过原子写入（临时文件 + `os.replace`）保存

**`<uploaded_files>` 标签处理**:
- 记忆更新时自动移除包含 `<uploaded_files>` 的整个段落
- 防止未来会话中出现对不存在文件的引用

**跨模块协作**:
- **MemoryUpdater ↔ UploadsMiddleware**: 清洗由上传中间件注入的内容
- **MemoryUpdater ↔ MemoryQueue**: 通过防抖队列接收待处理消息
- **MemoryUpdater ↔ MemoryStorage**: 原子写入到用户记忆文件

---

## 跨模块交互总览

```
HTTP POST /api/threads/{id}/uploads
    │
    ▼
Gateway Upload Router (uploads.py)
    │
    ├──▸ normalize_filename() ──── 安全清洗
    ├──▸ ensure_uploads_dir() ──── 创建目录
    │       │
    │       └──▸ ThreadDataMiddleware (已创建目录结构)
    │
    ├──▸ convert_file_to_markdown() ──── 文档转换
    │       │
    │       └──▸ FileConversion (pymupdf4llm / markitdown)
    │
    ├──▸ sandbox.update_file() ──── 同步到沙箱
    │       │
    │       └──▸ LocalSandboxProvider._build_thread_path_mappings()
    │           → /mnt/user-data/uploads/ 虚拟路径映射
    │
    ▼
UploadsMiddleware.before_agent()
    │
    ├──▸ 读取 additional_kwargs.files
    ├──▸ extract_outline() ──── 提取文档大纲
    ├──▸ 生成 <uploaded_files> 注入内容
    └──▸ 注入到第一条 HumanMessage
    │
    ▼
Agent 执行 (可访问上传文件)
    │
    ▼
MemoryMiddleware.after_agent()
    │
    ├──▸ 收集对话消息
    ├──▸ MemoryQueue (防抖 30s)
    ├──▸ MemoryUpdater (LLM 提取)
    │       │
    │       └──▸ _strip_upload_mentions_from_memory()
    │           → 移除上传引用
    │
    └──▸ 原子写入到 memory.json
```

---

## 深入阅读

| 模块内文档 | 路径 |
|-----------|------|
| 上传系统 | `docs/core/uploads/` |
| 沙箱系统 | `docs/core/sandbox/` |
| 记忆系统 | `docs/core/memory/` |
| 中间件系统 | `docs/core/agent/middlewares/` |
| 文件上传设计文档 | `docs/FILE_UPLOAD.md` |
| Agent 请求全流程 | `docs/lifecycle/01-agent-request-flow.md` |
| 记忆上下文链路 | `docs/lifecycle/03-memory-context-chain.md` |
