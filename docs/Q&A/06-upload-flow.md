# Q&A 06: 上传功能的流程

> 上传功能的完整处理流程是怎样的？第一步"创建 Agent"具体做了什么？

---

## 完整上传流程

```
用户选择文件 → 前端上传 → Gateway 存储 → 格式转换 → 沙箱同步
                                                      ↓
                              Agent 执行时 → UploadsMiddleware 注入上下文
```

---

## 第一步：文件接收（Gateway）

### 端点

```
POST /api/threads/{thread_id}/uploads
Content-Type: multipart/form-data
```

### 处理流程

```
1. 验证限制
   ├── 文件数量 ≤ 配置上限
   ├── 单文件大小 ≤ 50MB（默认）
   └── 总大小 ≤ 100MB（默认）

2. 安全处理
   ├── normalize_filename() — 清洗文件名，防止路径穿越
   ├── 符号链接检测 — 拒绝符号链接文件
   └── 路径安全验证 — 确保写入目标在合法目录内

3. 流式写入
   ├── _write_upload_file_with_limits() — 逐块写入
   └── 实时检查大小限制（写入过程中也可能超限）

4. 沙箱同步
   ├── sandbox.update_file() — 将文件同步到沙箱环境
   └── _make_file_sandbox_writable() — 设置可写权限
```

### 存储位置

| 视角 | 路径 |
|------|------|
| 宿主机 | `{workspace}/users/{uid}/threads/{tid}/user-data/uploads/{filename}` |
| 沙箱内 | `/mnt/user-data/uploads/{filename}` |

---

## 第二步：格式转换

当 `uploads.auto_convert_documents = true` 时，系统自动将文档转为 Markdown：

```
PDF    ──→ pymupdf4llm（优先）→ markitdown（兜底）
Word   ──→ markitdown
PPT    ──→ markitdown
Excel  ──→ markitdown
```

转换后的 `.md` 文件与原文件存储在同一目录。这步是**上传时立即执行**的，而非等到 Agent 运行时。

---

## 第三步：Agent 上下文注入（UploadsMiddleware）

### "创建 Agent"的含义

"创建 Agent"**不是指上传流程中的某个步骤**。准确地说：

- 上传发生在 Agent 运行**之前**（用户先上传文件，再发送消息）
- 当用户发送消息触发 Agent 运行时，`make_lead_agent()` 构建 Agent 实例
- `make_lead_agent()` 内部组装中间件链，**第一个中间件就是 `ThreadDataMiddleware`**，确保线程目录存在
- 随后 `UploadsMiddleware` 在 `before_agent` 钩子中注入文件上下文

### UploadsMiddleware 处理流程

```python
# middlewares/uploads_middleware.py — before_agent 钩子
def before_agent(self, state, runtime):
    # 1. 提取当前消息中的新文件
    new_files = extract_files_from_human_message(state)

    # 2. 扫描线程上传目录中的历史文件
    all_files = scan_upload_directory(thread_id)

    # 3. 对每个 Markdown 文件提取文档大纲
    for file in all_files:
        if file.endswith('.md'):
            outline = extract_outline(file)

    # 4. 构建 <uploaded_files> 上下文消息
    context_msg = format_upload_context(new_files, all_files, outlines)

    # 5. 注入到最后一个 HumanMessage 之前
    state.messages.insert(-1, context_msg)
```

### 注入的上下文格式

```xml
<uploaded_files>
  <file name="report.pdf" path="/mnt/user-data/uploads/report.pdf">
    <outline>
      1. 引言
      2. 方法论
      3. 实验结果
    </outline>
  </file>
  <file name="data.xlsx" path="/mnt/user-data/uploads/data.xlsx"/>
</uploaded_files>
```

Agent 通过这个上下文了解：
- 有哪些文件可用
- 文件在沙箱中的路径（可以直接读取）
- 文档的结构大纲（决定是否需要深入阅读）

---

## 完整时序图

```
时间线
  │
  ├─ 用户选择文件
  │     ↓
  ├─ POST /api/threads/{tid}/uploads
  │     ├── 文件名清洗
  │     ├── 流式写入磁盘
  │     ├── 沙箱同步
  │     └── 文档格式转换（如需要）
  │     ↓
  ├─ 返回 { files: [{ filename, size, virtual_path }] }
  │     ↓
  ├─ 前端更新乐观消息中的文件状态：uploading → uploaded
  │
  │  ···用户输入文本并发送···
  │
  ├─ POST /api/threads/{tid}/runs/stream
  │     ├── make_lead_agent() 构建 Agent
  │     │     ├── 组装中间件链
  │     │     │     ├── ThreadDataMiddleware（确保目录存在）
  │     │     │     ├── UploadsMiddleware（注入文件上下文）
  │     │     │     └── ...其他中间件
  │     │     └── 绑定工具和模型
  │     │
  │     ├── Agent 开始执行
  │     │     └── UploadsMiddleware.before_agent()
  │     │           ├── 扫描上传目录
  │     │           ├── 提取文档大纲
  │     │           └── 注入上下文消息
  │     │
  │     └── Agent 使用文件信息回答用户问题
  │
```

---

## 相关源码

| 组件 | 文件 |
|------|------|
| 上传路由 | `backend/app/gateway/routers/uploads.py` |
| 上传中间件 | `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py` |
| 文件转换 | `backend/packages/harness/deerflow/utils/file_conversion.py` |
| 文件名安全 | `backend/packages/harness/deerflow/utils/network.py` |
| 前端上传逻辑 | `frontend/src/core/uploads/` |

## 深入阅读

- [文件上传全流程](../docs/lifecycle/06-file-upload.md)
- [上传设计决策](../docs/core/uploads/01-design-decisions.md)
- [工具类实现分析](../docs/core/utils/02-implementation-analysis.md)
