# 上传系统 — 全局概览

## 定位

DeerFlow 上传模块（`deerflow.uploads`）提供文件上传的纯业务逻辑层，负责文件名安全校验、路径遍历防护、符号链接攻击防御、文件名冲突处理以及虚拟路径构建。该模块不依赖 FastAPI/HTTP，Gateway API 和嵌入式 DeerFlowClient 都委托给这些函数执行实际的文件操作。

> **关键边界**：上传模块只管理"文件的存储和安全"，不管理"文件的语义处理"（如文档转换，由 `deerflow.utils.file_conversion` 负责）。

## 源文件

```
backend/packages/harness/deerflow/uploads/
└── manager.py    # 所有上传核心逻辑
```

## 解决的核心问题

| 问题 | 上传模块的解决方案 |
|------|---------------------|
| **路径遍历攻击** | `validate_path_traversal()` 通过 `resolve().relative_to()` 验证文件路径始终在基础目录内 |
| **符号链接攻击** | `open_upload_file_no_symlink()` 使用 POSIX `O_NOFOLLOW` 或 Windows 双重 `lstat` 防止恶意符号链接覆盖沙箱外文件 |
| **文件名冲突** | `claim_unique_filename()` 自动追加 `_N` 后缀解决同批次上传中的重名问题 |
| **文件名规范化** | `normalize_filename()` 剥离目录组件、拒绝反斜杠、限制长度，确保文件名可安全用于文件系统 |
| **路径隔离** | 每个 thread 拥有独立的 uploads 目录，通过 `validate_thread_id()` 确保 thread ID 可安全用于路径构建 |
| **虚拟路径映射** | `upload_virtual_path()` / `upload_artifact_url()` 构建统一的前端访问路径和 artifact URL |

## 安全机制详解

### 1. 路径遍历防护

```python
validate_path_traversal(path, base)
```

- 对 `path` 和 `base` 都调用 `resolve()` 解析所有符号链接和 `..` 组件
- 使用 `relative_to()` 验证路径在基础目录内
- 失败时抛出 `PathTraversalError`

### 2. 符号链接攻击防护

上传目录可能被挂载到本地沙箱中。沙箱进程可以在未来上传文件名处放置符号链接，使 `Path.write_bytes` 跟随该链接覆盖上传目录外的文件。

**POSIX 策略（`O_NOFOLLOW`）**：

```python
# open() 遇到符号链接直接失败（ELOOP），不跟随
flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
fd = os.open(dest, flags, 0o600)

# 打开后再次验证
opened_stat = os.fstat(fd)
assert S_ISREG(opened_stat.st_mode)    # 必须是普通文件
assert opened_stat.st_nlink == 1       # 必须是独占的（无硬链接）
```

**Windows 策略（TOCTOU 缓解）**：

Windows 不支持 `O_NOFOLLOW`，因此采用多层防御：

1. **预检查 lstat**：`open()` 前立即 `lstat()` 检查目标是否为普通文件
2. **open 后 fstat 验证**：`open()` 后通过 `fstat()` 再次确认是普通文件
3. **硬链接检测**：`st_nlink > 1` 拒绝多链接文件
4. **路径遍历兜底**：即使 TOCTOU 窗口被利用，`validate_path_traversal` 阻止逃逸 `base_dir`

> **注意**：Windows 策略在 `lstat()` 和 `open()` 之间存在理论上的竞态窗口。路径遍历检查缓解了从 `base_dir` 逃逸的风险，但无法完全消除攻击者在检查后原子替换符号链接的可能性。

### 3. 文件名规范化

```python
normalize_filename(filename)
```

防护规则：

| 检查项 | 防护目标 |
|--------|----------|
| `Path(filename).name` | 剥离所有目录组件（`../../etc/passwd` → `passwd`） |
| 空值/`.`/`..` 检查 | 拒绝解析为空或遍历模式的文件名 |
| 反斜杠检查 | 拒绝 Windows 风格路径（`dir\file`） |
| UTF-8 字节长度 ≤ 255 | 确保文件名不超过文件系统限制 |

## 核心函数参考

### 文件名操作

| 函数 | 用途 | 返回值 |
|------|------|--------|
| `normalize_filename(filename)` | 清洗用户输入的文件名 | 安全的 basename 字符串 |
| `claim_unique_filename(name, seen)` | 解决文件名冲突，追加 `_N` 后缀 | 唯一文件名（已加入 `seen` 集合） |

### 文件操作

| 函数 | 用途 | 返回值 |
|------|------|--------|
| `open_upload_file_no_symlink(base_dir, filename)` | 安全打开上传目标文件（防符号链接） | `(Path, file_handle)` 元组 |
| `write_upload_file_no_symlink(base_dir, filename, data)` | 安全写入上传字节（防符号链接） | 目标文件 `Path` |
| `list_files_in_dir(directory)` | 列出目录中的文件（不跟随符号链接） | `{"files": [...], "count": N}` |
| `delete_file_safe(base_dir, filename, *, convertible_extensions)` | 安全删除文件（含路径验证和伴生 `.md` 清理） | `{"success": True, "message": ...}` |

### 路径构建

| 函数 | 用途 | 示例输出 |
|------|------|----------|
| `get_uploads_dir(thread_id)` | 获取 thread 的上传目录路径（无副作用） | `.deer-flow/.../uploads/` |
| `ensure_uploads_dir(thread_id)` | 确保上传目录存在（按需创建） | 同上 |
| `upload_virtual_path(filename)` | 构建虚拟路径 | `/mnt/user-data/uploads/file.pdf` |
| `upload_artifact_url(thread_id, filename)` | 构建 artifact API URL | `/api/threads/{id}/artifacts/.../file.pdf` |
| `enrich_file_listing(result, thread_id)` | 为文件列表添加虚拟路径和 artifact URL | 原地修改并返回 |

### 校验函数

| 函数 | 用途 | 异常 |
|------|------|------|
| `validate_thread_id(thread_id)` | 校验 thread ID 格式 | `ValueError`（含非法字符或为空） |
| `validate_path_traversal(path, base)` | 校验路径不逃逸基础目录 | `PathTraversalError` |

## 路径隔离模型

上传文件按 thread 和 user 隔离存储：

```
.deer-flow/
└── users/{user_id}/
    └── threads/{thread_id}/
        └── user-data/
            └── uploads/       ← 上传目录
                ├── report.pdf
                ├── report.md  ← 自动转换的 markdown
                └── data.xlsx
```

- `user_id` 通过 `get_effective_user_id()` 解析，无认证模式默认为 `"default"`
- `thread_id` 必须匹配正则 `^[a-zA-Z0-9._-]+$`（仅允许字母、数字、点、下划线、连字符）

## 虚拟路径系统

上传文件在前端和 Agent 上下文中通过虚拟路径引用，与物理存储路径解耦：

| 路径类型 | 格式 | 用途 |
|----------|------|------|
| 虚拟路径 | `/mnt/user-data/uploads/{filename}` | Agent 和 Sandbox 中引用文件 |
| Artifact URL | `/api/threads/{thread_id}/artifacts/mnt/user-data/uploads/{filename}` | 前端通过 Gateway API 下载文件 |

文件名在 artifact URL 中经过 `urllib.parse.quote()` 百分号编码，确保空格、`#`、`?` 等特殊字符安全传递。

## 生命周期

```
上传请求到达（Gateway API 或 DeerFlowClient）
    │
    ▼
validate_thread_id() — 校验 thread ID 格式
    │
    ▼
ensure_uploads_dir() — 确保上传目录存在
    │
    ▼
normalize_filename() — 清洗文件名
    │
    ▼
claim_unique_filename() — 处理同批次文件名冲突
    │
    ▼
open_upload_file_no_symlink() — 安全打开目标文件
    │
    ├── POSIX: O_NOFOLLOW 阻止符号链接
    └── Windows: 双重 lstat + fstat 缓解 TOCTOU
    │
    ▼
写入文件数据 → 关闭句柄
    │
    ▼
（可选）convert_file_to_markdown() — 文档自动转换
    │
    ▼
返回上传结果（含虚拟路径和 artifact URL）
```

### 列表/删除流程

```
list_uploads(thread_id)
    │
    ▼
list_files_in_dir(uploads_dir) — 不跟随符号链接地扫描目录
    │
    ▼
enrich_file_listing() — 添加虚拟路径和 artifact URL
    │
    ▼
返回 {"files": [...], "count": N}

delete_upload(thread_id, filename)
    │
    ▼
normalize_filename() — 清洗文件名
    │
    ▼
validate_path_traversal() — 路径遍历校验
    │
    ▼
delete_file_safe() — 删除文件 + 清理伴生 .md
    │
    ▼
返回 {"success": true, "message": "Deleted ..."}
```

## 设计决策

- **纯业务逻辑**：不引入 FastAPI 依赖，使 Gateway（HTTP）和 DeerFlowClient（嵌入）共享同一套安全逻辑
- **O_NOFOLLOW 优先**：POSIX 上使用内核级防护，比用户态检查更可靠
- **文件名百分号编码**：artifact URL 中的文件名使用 `quote(filename, safe='')` 编码，确保所有特殊字符都被转义
- **硬链接检测**：`st_nlink == 1`（POSIX）或 `st_nlink <= 1`（Windows）防止攻击者通过硬链接到敏感文件的多个路径写入
- **权限模式 0o600**：新创建的上传文件仅 owner 可读写，最小化权限暴露
