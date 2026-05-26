# 01 - 前端渲染逻辑

> 本文档描述 DeerFlow 前端如何处理 SSE 流式推送、消息分组与渲染、实时流式渲染、历史记录回放两条路径的设计。

---

## 全链路架构图

```
┌──────────┐  SSE    ┌──────────────┐  events  ┌──────────────┐  merge  ┌──────────────┐
│ Gateway  │ ──────▸ │ useStream    │ ────────▸│ mergeMessages│ ──────▸ │ MessageList  │
│ API      │         │ (LangGraph   │          │ (dedupe)     │         │ (group+render)│
│          │         │  SDK)        │          └──────────────┘         └──────┬───────┘
└──────────┘         └──────┬───────┘                                         │
                           │                                                   ▼
                    ┌──────┴───────┐                                  ┌──────────────┐
                    │ Three Sources│                                  │ MessageGroup │
                    │              │                                  │ Renderer     │
                    │ ① history    │                                  │              │
                    │ ② stream     │                                  │ human        │
                    │ ③ optimistic │                                  │ assistant    │
                    └──────────────┘                                  │ processing   │
                                                                      │ subagent     │
                                                                      │ present-files│
                                                                      │ clarification│
                                                                      └──────────────┘
```

---

## 一、SSE 事件接收 — useStream

**核心文件**: `frontend/src/core/threads/hooks.ts` → `useThreadStream()`

### 1.1 SSE 连接建立

前端使用 LangGraph SDK 的 `useStream` hook 建立 SSE 连接：

```typescript
const thread = useStream<AgentThreadState>({
  client: getAPIClient(isMock),
  assistantId: "lead_agent",
  threadId: threadId,
  reconnectOnMount: true,          // 组件挂载时自动重连
  fetchStateHistory: { limit: 1 }, // 拉取最近 1 条状态快照
});
```

### 1.2 事件类型处理

| 事件回调 | 对应 SSE 事件 | 处理逻辑 |
|---------|-------------|---------|
| `onCreated(meta)` | metadata | 设置 thread_id、run_id，初始化流状态 |
| `onLangChainEvent(event)` | events | 过滤 `on_tool_end` 事件，通知工具完成监听器 |
| `onUpdateEvent(data)` | updates | 检测标题变化、SummarizationMiddleware 事件 |
| `onCustomEvent(event)` | custom | 处理 `task_running`（子代理进度）、`llm_retry`（重试） |
| `onError(error)` | — | 重置乐观消息，显示错误 toast |
| `onFinish(state)` | end | 标记流完成，更新 UI 状态 |

### 1.3 支持的流模式

**文件**: `frontend/src/core/api/stream-mode.ts`

```typescript
SUPPORTED_RUN_STREAM_MODES = [
  "values",          // 完整线程状态快照
  "messages",        // 消息更新
  "messages-tuple",  // 消息对（AI delta + tool result）
  "updates",         // 状态增量更新
  "events",          // LangChain 事件
  "custom",          // 自定义事件（子代理进度等）
];
```

`sanitizeRunStreamOptions()` 过滤不支持的模式并发出一次性警告。

---

## 二、消息三源合并与去重

**核心文件**: `frontend/src/core/threads/hooks.ts` → `mergeMessages()`

### 2.1 三条消息来源

```
① historyMessages    — 历史加载的消息（来自 GET /runs/{rid}/messages）
② thread.messages    — 实时 SSE 流推送的消息
③ optimisticMessages — 用户发送后立即显示的乐观消息
```

### 2.2 合并策略

```typescript
function mergeMessages(history, thread, optimistic): Message[] {
  // thread 消息优先级最高，覆盖 history 中同 id 的消息
  // optimistic 消息追加到末尾
  // 通过 messageIdentity() 去重
}
```

**去重键**: 优先使用 `tool_call_id`（工具消息），回退到 `message.id`（普通消息）。

### 2.3 乐观消息

用户发送消息时，立即在 UI 中显示，不等服务端响应：

```typescript
const optimisticMessage = {
  type: "human",
  id: `opt-human-${Date.now()}`,
  content: text ? [{ type: "text", text }] : "",
};
```

当 SSE 流返回真正的消息后，乐观消息被替换。

---

## 三、消息分组系统

**核心文件**: `frontend/src/core/messages/utils.ts` → `getMessageGroups()`

### 3.1 分组类型

```typescript
type MessageGroup =
  | "human"                      // 用户消息（每组一条）
  | "assistant"                  // 普通 AI 文本回复
  | "assistant:processing"       // AI 含推理/工具调用的回复
  | "assistant:present-files"    // 文件展示（present_files 工具）
  | "assistant:clarification"    // 澄清请求（ask_clarification 工具）
  | "assistant:subagent"         // 子代理任务（task 工具）
```

### 3.2 分组逻辑

```
原始消息列表
    │
    ├─ 过滤隐藏消息 (isHiddenFromUIMessage)
    │   └─ metadata.hide_from_ui === "true"
    │   └─ name === "summary"（摘要消息）
    │   └─ name === "todo_reminder"（待办提醒）
    │
    ├─ 用户消息 → 每条独立成组 (human)
    │
    ├─ AI 消息 → 判断类型:
    │   ├─ 含 present_files 工具调用 → assistant:present-files
    │   ├─ 含 ask_clarification 工具调用 → assistant:clarification
    │   ├─ 含 task 工具调用 → assistant:subagent
    │   ├─ 含推理内容或工具调用 → assistant:processing
    │   └─ 纯文本 → assistant
    │
    └─ Tool 消息 → 附加到上一个未关闭的 processing 组
```

### 3.3 隐藏消息规则

| 隐藏条件 | 消息类型 | 来源 |
|---------|---------|------|
| `metadata.hide_from_ui === "true"` | todo_reminder 等 | TodoMiddleware |
| `name === "summary"` | 摘要消息 | SummarizationMiddleware |
| `metadata.caller?.startsWith("middleware:")` | 中间件内部消息 | 后端中间件 |

---

## 四、实时流式渲染

### 4.1 流式 Markdown 渲染

**核心文件**: `frontend/src/core/streamdown/plugins.ts` + `components/workspace/messages/markdown-content.tsx`

```
SSE delta (文本片段)
    ↓ 按 message.id 拼接
完整 Markdown 文本
    ↓ MessageResponse 组件
remark 解析 → rehype 转换 → React 组件树
```

**插件配置**:

| 场景 | remark 插件 | rehype 插件 | 特点 |
|------|-----------|-----------|------|
| AI 流式输出 | GFM, Math | Raw, KaTeX | 支持 HTML 标签和数学公式 |
| 中文动画 | +CJK 分词 | +Span 动画 | 逐字淡入效果 |
| 推理内容 | GFM, Math | KaTeX (无 Raw) | 防止推理文本中的幻觉 HTML |
| 用户消息 | Math | KaTeX | 最小化处理，无自动链接 |

### 4.2 增量渲染策略

LLM 的流式输出是**增量 delta**——同一 `message.id` 的多次 delta 需要拼接：

```
delta 1: "你好"
delta 2: "你好，我是"
delta 3: "你好，我是 DeerFlow"
```

LangGraph SDK 在内部按 `message.id` 合并 delta，前端拿到的是已拼接的消息列表。

### 4.3 流式指示器

流进行中时显示 `StreamingIndicator`（三个跳动圆点），位于消息组底部。

### 4.4 推理内容渲染

推理（thinking）内容从三个来源提取：
1. `additional_kwargs.reasoning_content` — 标准 thinking 字段
2. Content array 中的 `thinking` 属性 — Anthropic gateway 格式
3. `<thinkrangle/>` 标签 — 某些模型的 XML 格式

推理内容使用独立的 `Reasoning` 组件渲染（可折叠，使用 `reasoningPlugins` 排除 Raw 插件）。

---

## 五、历史记录回放路径

### 5.1 两种消息加载路径

```
路径 A：实时流（新对话）
  用户发送 → POST /runs/stream → SSE 推送 → useStream 接收 → 实时渲染

路径 B：历史回放（重新打开对话）
  打开线程 → GET /runs → 获取 run 列表
           → GET /runs/{rid}/messages → 加载每个 run 的消息
           → 合并去重 → 静态渲染
```

### 5.2 历史消息加载

**核心文件**: `frontend/src/core/threads/hooks.ts` → `useThreadHistory()`

```typescript
export function useThreadHistory(threadId: string) {
  // 1. 获取线程的所有 run
  const runs = useThreadRuns(threadId);

  // 2. 对每个 run 加载消息
  const loadMessages = async () => {
    const result = await fetch(
      `/api/threads/${threadId}/runs/${runId}/messages`
    );
    // 3. 过滤中间件内部消息
    const messages = result.data
      .filter(m => !m.metadata.caller?.startsWith("middleware:"));
    // 4. 去重后追加到历史消息列表
    setMessages(prev => dedupeMessagesByIdentity([...messages, ...prev]));
  };
}
```

### 5.3 分页与无限滚动

```
┌──────────────────────┐
│  IntersectionObserver │ ← 监听顶部元素进入视口
│  (LoadMore trigger)   │
├──────────────────────┤
│  ↑ loadMore()         │ ← 触发加载更早的消息
│  ┌──────────────────┐ │
│  │ ...older messages│ │
│  ├──────────────────┤ │
│  │ recent messages  │ │
│  ├──────────────────┤ │
│  │ streaming msg    │ │ ← 实时追加
│  └──────────────────┘ │
└──────────────────────┘
```

- **分页参数**: `has_more`（是否还有更早的消息）、`cursor`（游标位置）
- **节流**: 1200ms 间隔防止频繁加载
- **去重**: `dedupeMessagesByIdentity()` 基于 `messageIdentity()`

### 5.4 两条路径的统一

实时流和历史回放在 `mergeMessages()` 中统一：

```typescript
const mergedMessages = mergeMessages(
  historyMessages,     // 路径 B 加载的
  thread.messages,     // 路径 A 推送的（或重连后恢复的）
  optimisticMessages   // 路径 A 的乐观消息
);
```

`reconnectOnMount: true` 确保组件重新挂载时自动恢复流连接，实现两条路径的无缝切换。

---

## 六、工具模块渲染

### 6.1 渲染策略总览

| 工具类型 | 渲染方式 | 组件 |
|---------|---------|------|
| `task`（子代理） | SubtaskCard 进度卡片 | `SubtaskCard` |
| `present_files` | 文件列表 + 下载链接 | `ArtifactFileList` |
| `ask_clarification` | 独立 Markdown 块 | `MarkdownContent` |
| `bash`/`write_file` 等 | 折叠工具调用详情 | `Task` 折叠组件 |
| 图片文件 | 内联图片预览 | `RichFileCard` |

### 6.2 子代理任务渲染

**核心文件**: `frontend/src/components/workspace/messages/message-list.tsx`

```
assistant:subagent 消息组
    │
    ├─ 遍历 AI 消息中的 task 工具调用
    │   └─ 提取 task_id, description, subagent_type
    │
    ├─ 遍历 Tool 消息
    │   └─ 更新任务状态 (running → completed/failed/timed_out)
    │
    └─ 为每个任务渲染 SubtaskCard
        ├─ 标题: description
        ├─ 状态: spinner / check / error icon
        └─ 展开: 完整任务输出
```

### 6.3 文件上传渲染

**核心文件**: `frontend/src/components/workspace/messages/message-list-item.tsx`

```
RichFileCard
    ├─ 上传中 (status: "uploading")
    │   └─ Spinner + 文件名
    ├─ 图片文件
    │   └─ <img> 标签，最大宽度限制
    └─ 其他文件
        └─ 文件图标 + 文件名 + 大小 + 类型标签
```

### 6.4 链接与引用渲染

Markdown 中的特殊链接：

| 链接格式 | 渲染行为 |
|---------|---------|
| `citation:123` | `CitationLink` 组件（内联引用） |
| `/mnt/user-data/...` | 解析为 artifact URL（文件下载） |
| 外部 URL | 新窗口打开，`noopener noreferrer` |

---

## 七、状态管理架构

```
┌─────────────────────────────────────────────────────┐
│                    Thread State                      │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │ Server State │  │ Stream State │  │ Local State │ │
│  │ (TanStack    │  │ (useStream   │  │ (useState,  │ │
│  │  Query)      │  │  LangGraph)  │  │  useRef)    │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬─────┘ │
│         │                 │                  │       │
│         └────────┬────────┘                  │       │
│                  ▼                           │       │
│         ┌──────────────┐                     │       │
│         │ mergeMessages│ ◂───────────────────┘       │
│         │ (dedupe)     │  (optimistic messages)      │
│         └──────┬───────┘                             │
│                ▼                                      │
│         ┌──────────────┐                              │
│         │ getMessage   │                              │
│         │ Groups()     │                              │
│         └──────┬───────┘                              │
│                ▼                                      │
│         ┌──────────────┐                              │
│         │ MessageList  │                              │
│         │ (render)     │                              │
│         └──────────────┘                              │
└─────────────────────────────────────────────────────┘
```

### 状态分层

| 层 | 来源 | 特点 |
|---|------|------|
| Server State | TanStack Query | 缓存、自动失效、后台刷新 |
| Stream State | useStream (LangGraph SDK) | SSE 实时推送，自动重连 |
| Local State | React useState/useRef | 乐观消息、UI 状态、发送锁 |

### 关键 Ref 状态

- `pendingUsageBaselineMessageIdsRef`: Token 用量计算的基准消息 ID
- `messagesRef`: 消息缓存，用于与 history 对比避免重复渲染
- `sendInFlightRef`: 防止并发发送

---

## 八、Token 用量展示

**核心文件**: `frontend/src/core/threads/token-usage.ts` + `components/workspace/token-usage-indicator.tsx`

### 展示模式

| 模式 | 显示内容 |
|------|---------|
| `off` | 不显示 |
| `per_turn` | 每轮对话的 input/output tokens |
| `step_debug` | 逐步详细：每个 LLM 调用和工具调用的 token |

### 用量来源

- 实时流: SSE `end` 事件携带 `usage` 字段（按 message.id 去重统计一次）
- 历史记录: `GET /threads/{id}/token-usage` 聚合查询
- 子代理: 通过 `_subagent_usage_cache` 合并到父消息

---

## 九、组件层级速查

```
WorkspaceContainer
├── WorkspaceSidebar (线程列表)
├── ChatBox
│   ├── MessageList
│   │   ├── LoadMoreHistoryIndicator
│   │   ├── MessageListItem (human)
│   │   │   ├── RichFilesList
│   │   │   └── MarkdownContent
│   │   ├── MessageListItem (assistant)
│   │   │   ├── Reasoning (可折叠)
│   │   │   ├── MarkdownContent (流式)
│   │   │   └── Toolbar (复制/反馈)
│   │   ├── SubtaskCard (子代理任务)
│   │   ├── ArtifactFileList (文件展示)
│   │   └── StreamingIndicator (流式指示)
│   └── InputBox
│       ├── PromptInputFiles (文件上传)
│       └── ModelSelector (模型选择)
└── ArtifactSidebar (Artifact 侧边栏)
```

---

## 深入阅读

| 主题 | 文档 |
|------|------|
| Agent 请求全流程 | [docs/lifecycle/01-agent-request-flow.md](../../../docs/lifecycle/01-agent-request-flow.md) |
| SSE 流推送（后端） | [docs/core/runtime/05-event-streaming.md](../runtime/05-event-streaming.md) |
| 上下文压缩（摘要隐藏） | [docs/lifecycle/02-context-compression.md](../../../docs/lifecycle/02-context-compression.md) |
| 文件上传 | [docs/lifecycle/06-file-upload.md](../../../docs/lifecycle/06-file-upload.md) |
| 子代理事件 | [docs/lifecycle/05-subagent-dispatch.md](../../../docs/lifecycle/05-subagent-dispatch.md) |
