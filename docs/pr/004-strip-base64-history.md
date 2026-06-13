# 004 — fix(history): strip base64 image data from REST endpoint responses

| 字段 | 值 |
|------|-----|
| PR | #3535 |
| Commit | `09429644` |
| 状态 | Merged |
| 合并者 | WillemJiang |
| 合并时间 | 2026-06-13 |
| 分支 | fix/history-strip-base64 → upstream/main |
| 改动规模 | 6 文件 +230/-10 行 |

## 问题

Issue #3496：在对话中上传并查看多张图片后，刷新页面或重新进入会话时，
`/threads/{thread_id}/history` 接口响应极慢，前端页面卡死。

用户现象：上传图片 → 对话正常 → 刷新页面 → 白屏数秒甚至浏览器提示页面无响应。

## 根因

完整链路：

```
① view_image_tool 执行 → base64 写入 ThreadState.viewed_images
② ViewImageMiddleware.before_model() 构造 HumanMessage：
   content 含 image_url: data:image/png;base64,... （数 MB）
   标记 additional_kwargs={"hide_from_ui": True}
③ 消息随 LangGraph checkpoint 持久化
④ REST 端点读取 checkpoint → serialize_channel_values() 原样序列化
⑤ 完整 base64 数据通过 HTTP 返回给前端（数 MB → 数十 MB JSON）
⑥ 前端下载+解析巨大 JSON → 页面卡死
```

虽然前端 `isHiddenFromUIMessage()` 最终会过滤掉 `hide_from_ui` 消息不显示，
但下载和 JSON 解析已经完成，卡顿已经发生。

问题不止 `/history`：搜索 `serialize_channel_values()` 的所有消费者发现
**6 个 REST 端点**都有同样问题。

## 方案

在 `serialization.py` 中新增两个函数：

### `strip_data_url_image_blocks(messages)`

从 `hide_from_ui` 消息中移除 `data:` scheme 的 `image_url` content block：

- 只处理 `additional_kwargs.hide_from_ui is True` 的消息
- 只移除 `type == "image_url"` 且 URL 以 `data:` 开头的 content block
- 保留 text block、`https://` image URL、非 hide_from_ui 消息
- 消息数量和顺序不变

### `serialize_channel_values_for_api(channel_values)`

组合 wrapper：`serialize_channel_values` + `strip_data_url_image_blocks`，
供所有 REST 端点统一使用。

### 覆盖的 6 个端点

| 端点 | 路由文件 |
|------|---------|
| `GET /{thread_id}` | `threads.py` |
| `GET /{thread_id}/state` | `threads.py` |
| `POST /{thread_id}/state` | `threads.py` |
| `POST /{thread_id}/history` | `threads.py` |
| `POST /api/runs/wait` | `runs.py` |
| `POST /{thread_id}/runs/wait` | `thread_runs.py` |

## 取舍

| 选择 | 理由 |
|------|------|
| 只剥离 `data:` scheme，不剥离 `https://` | `https://` URL 指向外部资源，前端可以按需加载 |
| 只处理 `hide_from_ui` 消息 | 非 hidden 消息不应被篡改，保持最小影响面 |
| 新增 wrapper 而非修改 `serialize_channel_values` | SSE streaming 路径（`worker.py`）仍需完整 base64 传给模型 |
| 不完全删除 `hide_from_ui` 消息 | 保持消息数组结构不变，前端 dedup 逻辑依赖消息索引 |

**放弃的方案**：
- 在 `serialize_channel_values` 内加过滤 — 会影响 SSE streaming，模型运行时需要 base64
- 完全删除 `hide_from_ui` 消息 — 改变消息数量，可能影响前端逻辑
- 在 `ViewImageMiddleware` 不存储 base64 — 需要改变 checkpoint 结构，影响面太大
- 只修 `/history` 端点 — 初始实现遗漏了其他 5 个端点，扩大自审后全部修复

**SSE 路径隔离**：`worker.py` 的 SSE streaming 仍使用 `serialize_channel_values()`
原函数，base64 数据在模型运行时继续通过 SSE 传给前端，这是正确的行为——
模型在活跃运行中需要看到图片内容。

## Review 争议

WillemJiang 质疑 `ViewImageMiddleware` 没有设置 `hide_from_ui`。
fancy-agent 反驳并引用代码行号 `view_image_middleware.py:185` 证明
`hide_from_ui: True` 在 PR 之前就已存在。最终 WillemJiang 确认并合并。

教训：PR body 应引用精确行号，避免 reviewer 看到旧版本代码时产生误解。

## 验证

- 8 个新单元测试覆盖：
  - base64 移除
  - 非 hidden 消息保留
  - `https://` URL 保留
  - string content 处理
  - 非 dict 消息处理
  - 混合消息场景
  - `serialize_channel_values_for_api` wrapper
  - 无 messages 的 channel_values
- 61 个已有测试全部通过
- `test_harness_boundary.py` 通过（新增 harness 导出未破坏边界）
- `ruff check` + `ruff format --check` 通过
