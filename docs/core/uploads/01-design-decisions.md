# Uploads 设计决策

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

## 核心决策清单

| # | 决策 | 解决的问题 | 权衡 |
|---|------|-----------|------|
| 1 | markitdown 通用转换引擎 | 多格式统一处理 | PDF 质量不如专用引擎 |
| 2 | PDF 双引擎策略 | 文本型质量 + 图片型覆盖 | pymupdf4llm 可选安装 |
| 3 | 文件大小限制 50MB/100MB | 资源耗尽保护 | 大文件需自行预处理 |
| 4 | 路径遍历防护 normalize_filename | 恶意文件名逃逸目录 | 合法特殊字符被拒绝 |
| 5 | 同名去重 _N 后缀 | 批量上传同名互不覆盖 | 文件名可能不符预期 |
| 6 | 记忆清理上传提及 | 会话级引用不入长期记忆 | 正则可能误删合法句子 |

---

## 决策 1：markitdown 通用转换引擎

### 动机

用户上传格式多样（PDF/PPT/Excel/Word），LLM 无法处理二进制格式，需要统一转为 Markdown。选择微软 `markitdown` 库，基于 python-pptx、openpyxl、python-docx 等成熟库。

### 权衡

markitdown 对 PDF 的处理不如 pymupdf4llm 精细（标题检测、结构保持），因此 PDF 场景引入双引擎策略。markitdown 是纯 Python 实现，无系统依赖，部署简单。大文件通过 `asyncio.to_thread()` 后台线程转换避免阻塞事件循环。

---

## 决策 2：PDF 双引擎策略

### 动机

PDF 类型差异极大：文本型（Word/LaTeX 导出）最佳引擎是 pymupdf4llm；图片型（扫描件）需要 markitdown 的 OCR；加密型两者都可能失败。单一引擎无法覆盖所有场景。

### 设计选择

`auto` 模式（默认）自适应流程：先尝试 pymupdf4llm -> 检查输出稀疏度（chars/page < 50）-> 稀疏则降级到 markitdown。配置支持 `auto`（默认）、`pymupdf4llm`（强制，不降级）、`markitdown`（直接使用）三种模式。

### 权衡

pymupdf4llm 是可选依赖（`uv add pymupdf4llm`），未安装时自动降级到 markitdown，不影响基本功能。稀疏检测需要额外打开 PDF 获取页数，增加少量 I/O 开销。

---

## 决策 3：文件大小限制

### 动机

无限制上传导致磁盘耗尽、CPU/内存过载、SSE 超时。

### 设计选择

默认限制：单文件 50MB（`uploads.max_file_size`）、请求总 100MB（`uploads.max_total_size`）、数量 10 个（`uploads.max_files`）。检查采用流式策略：8KB chunk 边读边写边校验。

### 权衡

50MB 覆盖大多数文档但不够处理大型视频或数据集。超限时 `_cleanup_uploaded_paths` 按逆序清理已写入文件，实现 all-or-nothing 语义。

---

## 决策 4：路径遍历防护

### 动机

文件名是经典攻击向量：`../../etc/passwd`、`..\windows\system32\config\sam`。

### 设计选择

`normalize_filename()` 四层防护：`Path.name` 剥离目录 -> 拒绝 `.`/`..` -> 拒绝反斜杠（Windows 路径注入）-> UTF-8 字节 <= 255（ext4 限制）。

`validate_path_traversal()` 二次校验 `path.resolve().relative_to(base.resolve())`，形成纵深防御。`open_upload_file_no_symlink()` 在 POSIX 上使用 `O_NOFOLLOW` 内核级阻止符号链接攻击。

### 权衡

反斜杠拒绝意味着包含 `\` 的合法文件名（罕见）会被拒绝。纵深防御提供双重保护。

---

## 决策 5：同名去重 _N 后缀

### 动机

同一批次上传中同名文件直接覆盖会丢失数据，违反用户预期。

### 设计选择

`claim_unique_filename(name, seen)` 维护当前批次已使用文件名集合，追加序号（`report.pdf` -> `report_1.pdf`）。线性搜索第一个可用序号，通常冲突很少。

仅当前请求内去重。对历史同名文件采用覆盖行为（`ftruncate(0)`），避免文件名无限增长。

---

## 决策 6：记忆清理上传提及

### 动机

上传文件是会话级引用，路径 `/mnt/user-data/uploads/report.pdf` 仅在当前线程沙箱中有效。持久化到长期记忆后，Agent 会在后续不相关对话中搜索不存在的文件。

### 设计选择

`_strip_upload_mentions_from_memory()` 在 `MemoryUpdater._do_update()` 最后执行，从摘要和 facts 中移除上传事件句子（`uploaded ... file`、`/mnt/user-data/uploads/...`），但保留合法文件格式提及（"works with CSV files"、"Prefers PDF export"）。

### 权衡

正则可能误删合法句子（如"用户上传文件功能很满意"），但极少见。测试用例 `test_memory_upload_filtering.py` 验证了保留/删除的边界行为。
