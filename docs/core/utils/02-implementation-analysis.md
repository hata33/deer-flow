# 02 - 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/utils/` 源码逐层拆解实现细节。

---

## 一、模块结构

```
utils/
├── readability.py       # ReadabilityExtractor — HTML 正文提取 + Markdown 转换
├── file_conversion.py   # convert_file_to_markdown() — 文档格式转换
├── network.py           # 网络工具函数
└── ...
```

---

## 二、ReadabilityExtractor

### 2.1 提取流程

```
HTML 输入
    ↓
python-readability Document(html)
    ↓ summary()
提取正文 DOM
    ↓
markdownify 转换
    ↓
Markdown 输出
```

### 2.2 调用方

- **Jina AI 工具**: `jina_ai/tools.py` — Jina Client 返回 HTML → Readability 提取
- **InfoQuest 工具**: `infoquest/tools.py` — InfoQuest fetch 返回 HTML → Readability 提取
- **上传中间件**: `uploads_middleware.py` — 文档转换后的大纲提取

---

## 三、文件转换系统

### 3.1 convert_file_to_markdown()

```python
def convert_file_to_markdown(file_path) -> str:
    if is_pdf:
        return _convert_pdf_with_pymupdf4llm(file_path) or _convert_with_markitdown(file_path)
    return _convert_with_markitdown(file_path)  # PPT, Excel, Word
```

### 3.2 PDF 双引擎策略

```
PDF 文件
    ↓
_convert_pdf_with_pymupdf4llm()
    ├─ 成功且输出充分 (>50 chars/page)? → 返回
    └─ 失败或输出稀疏?
        ↓
    _convert_with_markitdown()  → 返回（兜底）
```

### 3.3 extract_outline()

从 Markdown 文本中提取标题结构作为文档大纲，供 `UploadsMiddleware` 注入到对话上下文中。

---

## 四、文件名安全

### normalize_filename()

```python
def normalize_filename(filename: str) -> str:
    # 1. 提取 basename（去除路径前缀）
    # 2. 移除控制字符和特殊字符
    # 3. 替换空格为下划线
    # 4. 确保非空（回退到 "file"）
```

被 Gateway 上传路由和 DeerFlowClient 共同使用。
