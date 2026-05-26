# Uploads 实现分析

> 本文档基于源码逐层拆解实现细节。回答"代码怎么写的、为什么这么写"。

## 分层总览

```
uploads/manager.py              # 核心业务逻辑（无 HTTP 依赖）
app/gateway/routers/uploads.py  # FastAPI 路由（HTTP 层）
utils/file_conversion.py        # 文档格式转换
agents/middlewares/uploads_middleware.py  # Agent 上下文注入
agents/memory/updater.py        # 记忆清理（_strip_upload_mentions）
```

---

## 1. 上传流程

### 完整处理链

```
POST /api/threads/{id}/uploads
├── 1. 验证文件数量限制（max_files=10）
├── 2. ensure_uploads_dir(thread_id) -> 创建目录
├── 3. 逐文件处理：
│   ├── normalize_filename() -> 安全文件名
│   ├── claim_unique_filename() -> 批次去重
│   ├── open_upload_file_no_symlink() -> 安全打开
│   ├── _write_upload_file_with_limits() -> 流式写入 + 大小检查
│   ├── convert_file_to_markdown() -> 可选文档转换
│   └── sandbox.update_file() -> 沙箱同步（如需要）
├── 4. 返回 UploadResponse
└── 5. 失败时 _cleanup_uploaded_paths() 清理已写入文件
```

### 流式大小检查

`_write_upload_file_with_limits()` 以 8KB chunk 边读边写边校验，确保不会因单个超大文件耗尽内存。超限时清理已写入的部分文件，实现 all-or-nothing 语义。

### 批次去重

`claim_unique_filename(name, seen)` 维护 `seen: set[str]`，同名文件追加 `_N` 序号。仅当前请求内去重，历史同名文件采用覆盖行为。

---

## 2. 文件转换实现

### convert_file_to_markdown()

公开入口函数。文件 > 1MB 时通过 `asyncio.to_thread()` 后台线程执行。转换结果写入同目录同名 `.md` 文件（如 `report.pdf` -> `report.md`）。

### _do_convert() 分支逻辑

非 PDF 直接用 markitdown；PDF 根据配置选择引擎：
- `markitdown`：直接使用
- `pymupdf4llm`：强制使用，不降级
- `auto`（默认）：先尝试 pymupdf4llm，稀疏则降级到 markitdown

### 稀疏检测

`_pymupdf_output_too_sparse()` 用 `pymupdf.open()` 获取页数，计算 `chars / pages`。低于 50 字符/页判定为图片型 PDF，降级到 markitdown。无法获取页数时回退到绝对阈值 200 字符。

---

## 3. 安全机制

### open_upload_file_no_symlink()

上传系统最关键的安全函数。上传目录可能被挂载到沙箱中，沙箱进程有机会在文件名处创建符号链接劫持写入。

**POSIX 路径**：`O_NOFOLLOW` 标志使 `os.open()` 在目标为符号链接时返回 `ELOOP`。打开后 `fstat` 验证是常规文件且 `st_nlink == 1`（无硬链接）。

**Windows 路径**：不支持 `O_NOFOLLOW`，双重 `lstat`（open 前后）+ `fstat` 验证缩小 TOCTOU 窗口，配合 `validate_path_traversal()` 纵深防御。

### 路径遍历防护

`normalize_filename()` 四层防护：`Path.name` 剥离目录 -> 拒绝 `.`/`..` -> 拒绝反斜杠 -> UTF-8 字节 <= 255。`validate_path_traversal()` 二次校验 `resolve().relative_to(base.resolve())`。

---

## 4. UploadsMiddleware 实现

### before_agent() 流程

1. 从 `message.additional_kwargs.files` 提取当前消息的新文件
2. 扫描 `uploads_dir` 获取历史文件（排除新文件）
3. 为每个文件提取 outline（转换生成的 `.md` 伴侣文件）或 preview（前 5 行）
4. 构建 `<uploaded_files>` 标签块（文件名、大小、虚拟路径、outline 行号）
5. prepend 到 `HumanMessage.content`（string 直接拼接，list 多模态插入 text block）

### 文件信息注入格式

```
<uploaded_files>
- report.pdf (1.2 MB)
  Path: /mnt/user-data/uploads/report.pdf
  Document outline (use `read_file` with line ranges):
    L1: Executive Summary
    L45: Financial Highlights

To work with these files:
- Read from the file first - use outline line numbers and `read_file`
- Use `grep` to search for keywords
</uploaded_files>
```

outline 提取调用 `extract_outline()`，识别三种 pymupdf4llm 标题样式：标准 `#` 标题、加粗结构性标题（SEC 文件）、分离式加粗标题（学术论文）。

---

## 5. 记忆清理与沙箱同步

### _strip_upload_mentions_from_memory()

在 `MemoryUpdater._do_update()` 最后一步执行。从所有摘要和 facts 中移除上传事件句子，但保留合法文件格式提及（"works with CSV files"）。处理范围包括 `user.topOfMind.summary`、`history.*` 等摘要字段，以及所有 fact 的 `content` 字段。

### 沙箱同步

`_uses_thread_data_mounts` 判断是否需要手动同步。LocalSandbox 直接映射目录无需同步；Docker AIO 通过 `sandbox.update_file()` 显式同步，`_make_file_sandbox_writable()` 添加 world-writable 权限防止用户权限不匹配。

### 伴侣文件清理

`delete_file_safe()` 删除原始文件时，若扩展名属于可转换类型（`.pdf`/`.docx` 等），同时清理同名的 `.md` 伴侣文件（如删除 `report.pdf` 同时删除 `report.md`）。
