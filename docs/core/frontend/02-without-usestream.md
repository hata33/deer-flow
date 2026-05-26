# 02 — 不使用 useStream 的流式实现指南

> 本文档描述如何不依赖 LangGraph SDK 的 `useStream` hook，使用原生 `fetch` + `ReadableStream` 实现完全等价的 SSE 流式对话功能。适合需要迁移到其他框架（Vue、Svelte）、使用非 React 环境（CLI、Node.js 脚本）、或希望完全掌控 SSE 细节的场景。

---

## 一、useStream 做了什么

`useStream` 是 `@langchain/langgraph-sdk/react` 提供的 React hook，封装了以下职责：

| 职责 | 描述 |
|------|------|
| **连接管理** | 调用 `client.runs.stream()` 发起 POST 请求，建立 SSE 长连接 |
| **SSE 解析** | 将 `text/event-stream` 响应解析为结构化事件 |
| **消息增量累积** | 将同一 `message.id` 的多次 delta 拼接为完整消息 |
| **状态快照** | 维护完整的线程状态（messages、title、artifacts 等） |
| **重连恢复** | 通过 `Last-Event-ID` + `streamResumable` 实现断点续传 |
| **生命周期回调** | 提供 `onCreated`、`onFinish`、`onError` 等回调 |
| **提交消息** | `thread.submit()` 发送用户消息并触发新 run |

以下章节逐项拆解，展示如何用原生 API 替代每一层。

---

## 二、SSE 协议基础

### 2.1 后端 SSE 格式

后端 `format_sse()` 函数输出标准 SSE 帧：

```
event: {事件类型}
data: {JSON 负载}
id: {事件ID}          ← 可选

                      ← 空行分隔
```

心跳帧（15 秒无事件时发送）：

```
: heartbeat

```

### 2.2 事件类型一览

| SSE event | 方向 | data 内容 | 说明 |
|-----------|------|-----------|------|
| `metadata` | 服务端→客户端 | `{run_id, thread_id}` | 流开始，标识当前 run |
| `values` | 服务端→客户端 | 完整 `AgentThreadState` | 全量状态快照 |
| `messages` | 服务端→客户端 | 消息 delta 或完整消息 | AI 文本增量、工具调用/结果 |
| `updates` | 服务端→客户端 | `{node_name: writes}` | 节点执行更新 |
| `events` | 服务端→客户端 | LangChain 事件 | `on_tool_end` 等 |
| `custom` | 服务端→客户端 | 自定义数据 | `task_running`、`llm_retry` |
| `error` | 服务端→客户端 | `{message, name}` | 错误信息 |
| `end` | 服务端→客户端 | `null` | 流结束 |
| heartbeat | 服务端→客户端 | （注释，无 data） | 保持连接 |

### 2.3 一个完整的 SSE 会话示例

```
event: metadata
data: {"run_id":"abc-123","thread_id":"thread-456"}
id: 1748000000-0

event: values
data: {"messages":[{"type":"human","id":"msg-1","content":"hello"}],"title":"","artifacts":[]}
id: 1748000000-1

event: messages
data: {"type":"ai","id":"msg-2","content":[{"type":"text","text":"你"}]}
id: 1748000000-2

event: messages
data: {"type":"ai","id":"msg-2","content":[{"type":"text","text":"你好"}]}
id: 1748000000-3

event: messages
data: {"type":"ai","id":"msg-2","content":[{"type":"text","text":"你好，我是"}]}
id: 1748000000-4

event: values
data: {"messages":[{"type":"human","id":"msg-1","content":"hello"},{"type":"ai","id":"msg-2","content":[{"type":"text","text":"你好，我是 DeerFlow"}]}],"title":"","artifacts":[]}
id: 1748000000-5

event: end
data: null
id: 1748000000-6

```

---

## 三、原生实现：连接与解析

### 3.1 SSE 解析器

SSE 协议是基于文本行的，需要处理事件跨 chunk 分割的情况：

```typescript
interface SSEEvent {
  event: string;
  data: string;
  id?: string;
}

class SSEParser {
  private buffer = "";
  private currentEvent: Partial<SSEEvent> = {};

  /** 将原始文本 chunk 喂入解析器，返回已完成的 SSE 事件 */
  feed(chunk: string): SSEEvent[] {
    this.buffer += chunk;
    const events: SSEEvent[] = [];

    while (true) {
      const newlineIndex = this.buffer.indexOf("\n");
      if (newlineIndex === -1) break;

      const line = this.buffer.slice(0, newlineIndex);
      this.buffer = this.buffer.slice(newlineIndex + 1);

      // 空行 = 事件结束
      if (line === "" || line === "\r") {
        if (this.currentEvent.event && this.currentEvent.data !== undefined) {
          events.push({
            event: this.currentEvent.event,
            data: this.currentEvent.data,
            id: this.currentEvent.id,
          });
        }
        this.currentEvent = {};
        continue;
      }

      // 注释行（心跳）
      if (line.startsWith(":")) {
        continue;
      }

      // 解析字段
      const colonIndex = line.indexOf(":");
      if (colonIndex === -1) continue;

      const field = line.slice(0, colonIndex);
      let value = line.slice(colonIndex + 1);
      if (value.startsWith(" ")) value = value.slice(1);

      switch (field) {
        case "event":
          this.currentEvent.event = value;
          break;
        case "data":
          // data 可以多行，用 \n 连接
          this.currentEvent.data =
            (this.currentEvent.data ?? "") + value;
          break;
        case "id":
          this.currentEvent.id = value;
          break;
      }
    }

    return events;
  }
}
```

### 3.2 建立流连接并发送消息

DeerFlow 的 SSE 端点是通过 POST 请求触发的。URL 格式：

```
POST {langgraph_base_url}/threads/{thread_id}/runs/stream
```

请求体包含 assistant 配置、输入消息和流模式：

```typescript
interface StreamRunOptions {
  threadId: string;
  assistantId: string;
  input: {
    messages: Array<{
      type: "human";
      content: string | Array<{ type: "text"; text: string }>;
      additional_kwargs?: Record<string, unknown>;
    }>;
  };
  config?: {
    recursion_limit?: number;
    configurable?: Record<string, unknown>;
  };
  context?: Record<string, unknown>;
  streamMode?: string[];
  streamSubgraphs?: boolean;
  streamResumable?: boolean;
}

async function startStream(
  baseUrl: string,
  options: StreamRunOptions,
  csrfToken?: string,
): Promise<{ response: Response; lastEventId: string }> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(
    `${baseUrl}/threads/${encodeURIComponent(options.threadId)}/runs/stream`,
    {
      method: "POST",
      headers,
      credentials: "include",
      body: JSON.stringify({
        assistant_id: options.assistantId,
        input: options.input,
        config: options.config,
        context: options.context,
        stream_mode: options.streamMode ?? [
          "values",
          "messages",
          "updates",
          "events",
          "custom",
        ],
        stream_subgraphs: options.streamSubgraphs ?? true,
        stream_resumable: options.streamResumable ?? true,
      }),
    },
  );

  if (!response.ok) {
    throw new Error(`Stream request failed: ${response.status}`);
  }

  return { response, lastEventId: "" };
}
```

### 3.3 消费 SSE 流

```typescript
interface StreamCallbacks {
  onMetadata: (meta: { run_id: string; thread_id: string }) => void;
  onValues: (state: Record<string, unknown>) => void;
  onMessages: (message: unknown) => void;
  onUpdates: (data: Record<string, unknown>) => void;
  onEvents: (event: { event: string; name: string; data: unknown }) => void;
  onCustom: (event: unknown) => void;
  onError: (error: { message: string; name?: string }) => void;
  onEnd: () => void;
}

let lastEventId = "";

async function consumeStream(
  response: Response,
  callbacks: StreamCallbacks,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No readable stream");

  const decoder = new TextDecoder();
  const parser = new SSEParser();

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      const chunk = decoder.decode(value, { stream: true });
      const events = parser.feed(chunk);

      for (const sseEvent of events) {
        if (sseEvent.id) {
          lastEventId = sseEvent.id;
        }

        switch (sseEvent.event) {
          case "metadata":
            callbacks.onMetadata(JSON.parse(sseEvent.data));
            break;
          case "values":
            callbacks.onValues(JSON.parse(sseEvent.data));
            break;
          case "messages":
            callbacks.onMessages(JSON.parse(sseEvent.data));
            break;
          case "updates":
            callbacks.onUpdates(JSON.parse(sseEvent.data));
            break;
          case "events":
            callbacks.onEvents(JSON.parse(sseEvent.data));
            break;
          case "custom":
            callbacks.onCustom(JSON.parse(sseEvent.data));
            break;
          case "error":
            callbacks.onError(JSON.parse(sseEvent.data));
            break;
          case "end":
            callbacks.onEnd();
            return;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
```

---

## 四、消息增量累积

这是 `useStream` 最核心的抽象之一。LLM 的文本输出是逐 token 的 delta，同一 `message.id` 的多次 delta 需要拼接。

### 4.1 消息累积器

```typescript
interface Message {
  type: string;
  id?: string;
  content: string | Array<{ type: string; text?: string }>;
  tool_call_id?: string;
  additional_kwargs?: Record<string, unknown>;
  tool_calls?: Array<{
    id: string;
    name: string;
    args: string;
  }>;
}

class MessageAccumulator {
  private messages = new Map<string, Message>();

  /** 处理 values 事件中的完整状态快照 */
  handleValues(state: { messages?: Message[] }): Message[] {
    if (state.messages) {
      for (const msg of state.messages) {
        if (msg.id) {
          this.messages.set(msg.id, structuredClone(msg));
        }
      }
    }
    return this.getMessages();
  }

  /** 处理 messages 事件中的 delta */
  handleDelta(delta: Message): Message[] {
    const id = delta.id;
    if (!id) {
      // 无 id 的消息直接追加（工具结果可能无 id）
      const tempId = `temp-${Date.now()}`;
      this.messages.set(tempId, delta);
      return this.getMessages();
    }

    const existing = this.messages.get(id);
    if (!existing) {
      // 新消息
      this.messages.set(id, structuredClone(delta));
      return this.getMessages();
    }

    // 合并 delta 到已有消息
    this.mergeDelta(existing, delta);
    return this.getMessages();
  }

  private mergeDelta(target: Message, delta: Message): void {
    // 合并文本 content
    if (
      Array.isArray(target.content) &&
      Array.isArray(delta.content)
    ) {
      for (const part of delta.content) {
        if (part.type === "text" && typeof part.text === "string") {
          const existingText = target.content.find(
            (p) => p.type === "text",
          );
          if (existingText && typeof existingText.text === "string") {
            existingText.text += part.text;
          } else {
            target.content.push(part);
          }
        } else {
          target.content.push(part);
        }
      }
    } else if (typeof target.content === "string" && typeof delta.content === "string") {
      target.content += delta.content;
    }

    // 合并 tool_calls
    if (delta.tool_calls) {
      if (!target.tool_calls) {
        target.tool_calls = [];
      }
      for (const tc of delta.tool_calls) {
        const existing = target.tool_calls.find((t) => t.id === tc.id);
        if (existing) {
          existing.args += tc.args; // tool_call args 是增量 JSON 片段
        } else {
          target.tool_calls.push(structuredClone(tc));
        }
      }
    }

    // 合并 additional_kwargs
    if (delta.additional_kwargs) {
      target.additional_kwargs = {
        ...target.additional_kwargs,
        ...delta.additional_kwargs,
      };
    }
  }

  getMessages(): Message[] {
    return Array.from(this.messages.values());
  }

  reset(): void {
    this.messages.clear();
  }
}
```

### 4.2 为什么需要累积

```
event: messages    → { id: "msg-2", content: [{ text: "你" }] }
event: messages    → { id: "msg-2", content: [{ text: "好" }] }  ← 追加
event: messages    → { id: "msg-2", content: [{ text: "，我是 DeerFlow" }] }
event: values      → { messages: [..., { id: "msg-2", content: "你好，我是 DeerFlow" }] }
                                                                    ↑ 完整状态覆盖
```

`values` 事件提供完整的线程状态快照，`messages` 事件提供增量 delta。当 `values` 到达时，它会覆盖之前累积的 delta。两种事件交替到达，最终结果是相同的。

---

## 五、重连与断点续传

### 5.1 使用 Last-Event-ID

后端 `MemoryStreamBridge` 在内存中保留最近 256 个事件。客户端在重连时发送 `Last-Event-ID` header，后端从该 ID 之后继续推送：

```typescript
async function reconnectStream(
  baseUrl: string,
  threadId: string,
  runId: string,
  lastEventId: string,
  csrfToken?: string,
): Promise<Response> {
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
  };
  if (csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  if (lastEventId) {
    headers["Last-Event-ID"] = lastEventId;
  }

  const response = await fetch(
    `${baseUrl}/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/join`,
    {
      method: "GET",
      headers,
      credentials: "include",
    },
  );

  if (!response.ok) {
    throw new Error(`Reconnect failed: ${response.status}`);
  }

  return response;
}
```

### 5.2 自动重连管理

```typescript
class StreamConnection {
  private lastEventId = "";
  private runId = "";
  private threadId = "";
  private abortController: AbortController | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;

  async connect(
    baseUrl: string,
    options: StreamRunOptions,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    this.abortController = new AbortController();

    const { response } = await startStream(baseUrl, options);
    await this.consumeWithReconnect(baseUrl, response, callbacks);
  }

  private async consumeWithReconnect(
    baseUrl: string,
    response: Response,
    callbacks: StreamCallbacks,
  ): Promise<void> {
    try {
      await consumeStream(response, {
        ...callbacks,
        onMetadata: (meta) => {
          this.runId = meta.run_id;
          this.threadId = meta.thread_id;
          this.reconnectAttempts = 0;
          callbacks.onMetadata(meta);
        },
        onEnd: () => {
          this.reconnectAttempts = 0;
          callbacks.onEnd();
        },
        onError: (err) => {
          callbacks.onError(err);
        },
      });
    } catch (error) {
      if (this.abortController?.signal.aborted) return;

      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++;
        const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30000);
        await new Promise((r) => setTimeout(r, delay));

        try {
          const reconnectResponse = await reconnectStream(
            baseUrl,
            this.threadId,
            this.runId,
            this.lastEventId,
          );
          await this.consumeWithReconnect(baseUrl, reconnectResponse, callbacks);
        } catch {
          // 重连失败，继续重试
          return this.consumeWithReconnect(baseUrl, response, callbacks);
        }
      } else {
        callbacks.onError({
          message: `Max reconnection attempts (${this.maxReconnectAttempts}) exceeded`,
        });
      }
    }
  }

  disconnect(): void {
    this.abortController?.abort();
    this.abortController = null;
  }
}
```

---

## 六、三源合并与去重

useStream 的 `thread.messages` 只包含实时流消息。前端还维护了两条额外来源，需要在展示层合并。

### 6.1 去重键

复用 `messageIdentity()` 的逻辑：

```typescript
function messageIdentity(message: Message): string | undefined {
  if (message.tool_call_id) {
    return `tool:${message.tool_call_id}`;
  }
  if (message.id) {
    return `message:${message.id}`;
  }
  return undefined;
}
```

工具消息用 `tool_call_id` 去重（同一个工具调用只有一个结果），普通消息用 `message.id` 去重。

### 6.2 合并策略

```
historyMessages  — 历史加载的消息（GET /runs/{rid}/messages）
threadMessages   — 实时 SSE 流推送的消息（来自 MessageAccumulator）
optimisticMessages — 乐观消息（用户发送后立即显示的占位消息）

合并规则：
1. 从 history 尾部向前扫描，去除已出现在 thread 中的消息（后端流消息覆盖历史）
2. 三条来源拼接：history(去重叠) + thread + optimistic
3. 全局去重（保留最后出现的）
```

```typescript
function mergeMessages(
  historyMessages: Message[],
  threadMessages: Message[],
  optimisticMessages: Message[],
): Message[] {
  const threadIds = new Set(
    threadMessages
      .map(messageIdentity)
      .filter((id): id is string => Boolean(id)),
  );

  // 从尾部去除已存在于 thread 的消息
  let cutoff = historyMessages.length;
  for (let i = historyMessages.length - 1; i >= 0; i--) {
    const identity = messageIdentity(historyMessages[i]!);
    if (identity && threadIds.has(identity)) {
      cutoff = i;
    } else {
      break;
    }
  }

  const combined = [
    ...historyMessages.slice(0, cutoff),
    ...threadMessages,
    ...optimisticMessages,
  ];

  // 全局去重：保留每个 identity 最后一次出现
  const lastIdx = new Map<string, number>();
  combined.forEach((msg, i) => {
    const id = messageIdentity(msg);
    if (id) lastIdx.set(id, i);
  });

  return combined.filter(
    (msg, i) => {
      const id = messageIdentity(msg);
      return !id || lastIdx.get(id) === i;
    },
  );
}
```

### 6.3 乐观消息

用户发送消息时，不等服务端响应，立即插入一条临时消息：

```typescript
function createOptimisticMessage(text: string): Message {
  return {
    type: "human",
    id: `opt-human-${Date.now()}`,
    content: text ? [{ type: "text", text }] : "",
  };
}
```

当 SSE 流返回真正的 `type: "human"` 消息后（通过 human 消息计数变化检测），乐观消息被清除。

---

## 七、历史记录加载

重新打开对话时，需要加载已有的消息。

### 7.1 加载流程

```
1. GET {backend_url}/api/threads/{thread_id}/runs
   → 获取所有 run 列表

2. GET {backend_url}/api/threads/{thread_id}/runs/{run_id}/messages
   → 获取每个 run 的消息

3. 过滤中间件内部消息：metadata.caller?.startsWith("middleware:")
4. 去重后合并到历史消息列表
```

### 7.2 原生实现

```typescript
async function loadThreadHistory(
  backendUrl: string,
  threadId: string,
): Promise<Message[]> {
  // 1. 获取 run 列表（通过 LangGraph SDK 的 API）
  const runsResponse = await fetch(
    `${langgraphUrl}/threads/${threadId}/runs`,
    { credentials: "include" },
  );
  const runs = await runsResponse.json();

  // 2. 按逆序加载每个 run 的消息（最新优先）
  const allMessages: Message[] = [];
  const loadedRunIds = new Set<string>();

  for (const run of runs.reverse()) {
    if (loadedRunIds.has(run.run_id)) continue;

    const result = await fetch(
      `${backendUrl}/api/threads/${threadId}/runs/${run.run_id}/messages`,
      { credentials: "include" },
    ).then((r) => r.json());

    const messages = result.data
      .filter((m) => !m.metadata?.caller?.startsWith("middleware:"))
      .map((m) => m.content);

    allMessages.push(...messages);
    loadedRunIds.add(run.run_id);
  }

  // 3. 去重
  return dedupeMessagesByIdentity(allMessages);
}
```

---

## 八、完整的状态管理

### 8.1 ThreadState 管理器

将上述所有组件组合为一个完整的状态管理器：

```typescript
interface ThreadState {
  messages: Message[];
  title: string;
  artifacts: string[];
  isLoading: boolean;
  error: string | null;
}

class ThreadStateManager {
  private accumulator = new MessageAccumulator();
  private connection: StreamConnection | null = null;
  private state: ThreadState = {
    messages: [],
    title: "",
    artifacts: [],
    isLoading: false,
    error: null,
  };
  private listeners = new Set<() => void>();

  // React 风格的订阅接口（可用于任何框架的状态绑定）
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private notify(): void {
    for (const listener of this.listeners) listener();
  }

  getState(): ThreadState {
    return this.state;
  }

  /** 发送消息并开始流式接收 */
  async sendMessage(
    langgraphUrl: string,
    backendUrl: string,
    threadId: string,
    text: string,
    context: Record<string, unknown>,
  ): Promise<void> {
    // 1. 创建乐观消息
    const optimistic = createOptimisticMessage(text);

    // 2. 先加载历史
    const history = await loadThreadHistory(backendUrl, threadId);

    // 3. 发起流
    this.state.isLoading = true;
    this.state.error = null;
    this.updateDisplayMessages(history, [], [optimistic]);
    this.notify();

    this.connection = new StreamConnection();
    await this.connection.connect(
      langgraphUrl,
      {
        threadId,
        assistantId: "lead_agent",
        input: {
          messages: [
            {
              type: "human",
              content: [{ type: "text", text }],
            },
          ],
        },
        config: { recursion_limit: 1000 },
        context,
        streamMode: ["values", "messages", "updates", "events", "custom"],
        streamSubgraphs: true,
        streamResumable: true,
      },
      {
        onMetadata: () => {},
        onValues: (state) => {
          const msgs = this.accumulator.handleValues(state as { messages?: Message[] });
          this.state.title = (state.title as string) ?? this.state.title;
          this.state.artifacts = (state.artifacts as string[]) ?? this.state.artifacts;
          this.updateDisplayMessages(history, msgs, []);
        },
        onMessages: (delta) => {
          const msgs = this.accumulator.handleDelta(delta as Message);
          this.updateDisplayMessages(history, msgs, []);
        },
        onUpdates: (data) => {
          // 检测标题变化
          for (const update of Object.values(data)) {
            if (update && typeof update === "object" && "title" in update) {
              this.state.title = (update as { title: string }).title;
            }
          }
        },
        onEvents: (event) => {
          if (event.event === "on_tool_end") {
            // 处理工具完成事件
          }
        },
        onCustom: (event) => {
          if (typeof event === "object" && event !== null && "type" in event) {
            const e = event as { type: string };
            if (e.type === "task_running") {
              // 更新子代理任务状态
            }
            if (e.type === "llm_retry") {
              // 显示重试提示
            }
          }
        },
        onError: (error) => {
          this.state.isLoading = false;
          this.state.error = error.message;
          this.notify();
        },
        onEnd: () => {
          this.state.isLoading = false;
          this.updateDisplayMessages(history, this.accumulator.getMessages(), []);
          this.notify();
        },
      },
    );
  }

  /** 合并三源消息并更新 state */
  private updateDisplayMessages(
    history: Message[],
    streamMessages: Message[],
    optimistic: Message[],
  ): void {
    this.state.messages = mergeMessages(history, streamMessages, optimistic);
    this.notify();
  }

  disconnect(): void {
    this.connection?.disconnect();
    this.state.isLoading = false;
    this.notify();
  }
}
```

### 8.2 与框架集成

**React**:

```typescript
function useThreadStreamManual(threadId: string) {
  const managerRef = useRef(new ThreadStateManager());
  const [state, setState] = useState(managerRef.current.getState());

  useEffect(() => {
    const unsubscribe = managerRef.current.subscribe(() => {
      setState({ ...managerRef.current.getState() });
    });
    return unsubscribe;
  }, []);

  return {
    ...state,
    sendMessage: (text: string, context: Record<string, unknown>) =>
      managerRef.current.sendMessage(langgraphUrl, backendUrl, threadId, text, context),
    disconnect: () => managerRef.current.disconnect(),
  };
}
```

**Vue 3**:

```typescript
function useThreadStreamVue(threadId: Ref<string>) {
  const manager = new ThreadStateManager();
  const messages = ref(manager.getState().messages);
  const isLoading = ref(false);
  const title = ref("");

  manager.subscribe(() => {
    const state = manager.getState();
    messages.value = state.messages;
    isLoading.value = state.isLoading;
    title.value = state.title;
  });

  async function sendMessage(text: string, context: Record<string, unknown>) {
    return manager.sendMessage(langgraphUrl, backendUrl, threadId.value, text, context);
  }

  return { messages, isLoading, title, sendMessage };
}
```

**Vanilla JS / Node.js CLI**:

```typescript
const manager = new ThreadStateManager();
manager.subscribe(() => {
  const { messages } = manager.getState();
  // 渲染最后一条 AI 消息
  const lastAi = messages.filterLast((m) => m.type === "ai");
  if (lastAi) process.stdout.write(`\r${extractText(lastAi)}`);
});

await manager.sendMessage(langgraphUrl, backendUrl, threadId, "Hello", {});
```

---

## 九、useStream vs 原生实现对照表

| 功能 | useStream 实现 | 原生实现 |
|------|---------------|---------|
| SSE 连接 | `client.runs.stream()` 自动处理 | `fetch()` + `response.body.getReader()` |
| 事件解析 | SDK 内部 SSE 解析器 | 自定义 `SSEParser` 类 |
| 消息累积 | `thread.messages` 自动维护 | `MessageAccumulator` 类 |
| 状态管理 | hook 内 `useState` | `ThreadStateManager` 类 |
| 重连 | `reconnectOnMount: true` | `StreamConnection` + `Last-Event-ID` |
| 乐观 UI | 外部 `optimisticMessages` state | 三源合并 `mergeMessages()` |
| 历史加载 | `useThreadHistory` hook | `loadThreadHistory()` 函数 |
| 生命周期 | `onCreated/onFinish/onError` 回调 | `StreamCallbacks` 接口 |
| CSRF | `onRequest` 注入到 SDK | 手动读取 cookie 并设置 header |
| 提交消息 | `thread.submit()` | 直接 `fetch()` POST |

---

## 十、关键注意事项

### 10.1 stream_mode 必须与后端协商

请求体中的 `stream_mode` 决定了会收到哪些事件类型。DeerFlow 前端请求了 5 种模式：

```json
{
  "stream_mode": ["values", "messages", "updates", "events", "custom"]
}
```

- `values` — 全量状态快照（包含所有消息、标题、artifacts）
- `messages` — 消息增量 delta
- `updates` — 节点执行更新（含标题变化）
- `events` — LangChain 事件（含 `on_tool_end`）
- `custom` — 自定义事件（`task_running`、`llm_retry`）

最少只需 `values` 即可工作，但没有增量 delta 的流式效果。

### 10.2 心跳处理

后端每 15 秒发送心跳注释 `: heartbeat\n\n`。`SSEParser` 应忽略这些行。如果应用层需要连接存活检测，可以记录最后一次收到事件的时间，超时后主动重连。

### 10.3 event_id 的持久化

`lastEventId` 需要在组件生命周期外持久化（如 `sessionStorage`），以支持页面刷新后重连。useStream 通过 `streamResumable: true` 和 `reconnectOnMount: true` 自动处理。

### 10.4 并发发送防护

同时发送多条消息会导致消息顺序错乱和状态不一致。使用 `sendInFlightRef` 模式防止并发：

```typescript
if (sendInFlightRef.current) return;
sendInFlightRef.current = true;
try {
  // ... 发送逻辑
} finally {
  sendInFlightRef.current = false;
}
```

---

## 十一、文件上传集成

发送带附件的消息时，需要先上传文件再发送文本：

```
1. 将文件转为 File 对象
2. POST {backend_url}/api/threads/{thread_id}/uploads → 获取 virtual_path
3. 在消息的 additional_kwargs.files 中附带文件信息
4. 发起 SSE 流
```

```typescript
async function sendMessageWithFiles(
  langgraphUrl: string,
  backendUrl: string,
  threadId: string,
  text: string,
  files: File[],
): Promise<void> {
  // 1. 上传文件
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  const uploadResult = await fetch(
    `${backendUrl}/api/threads/${threadId}/uploads`,
    {
      method: "POST",
      credentials: "include",
      body: formData,
    },
  ).then((r) => r.json());

  // 2. 构建文件元数据
  const fileMetadata = uploadResult.files.map((f: { filename: string; size: number; virtual_path: string }) => ({
    filename: f.filename,
    size: f.size,
    path: f.virtual_path,
    status: "uploaded",
  }));

  // 3. 发送消息（含文件元数据）
  await startStream(langgraphUrl, {
    threadId,
    assistantId: "lead_agent",
    input: {
      messages: [
        {
          type: "human",
          content: [{ type: "text", text }],
          additional_kwargs: { files: fileMetadata },
        },
      ],
    },
    // ...
  });
}
```

---

## 深入阅读

| 主题 | 文档 |
|------|------|
| 前端渲染逻辑（useStream 版本） | [01-rendering-logic.md](01-rendering-logic.md) |
| 后端 SSE 事件推送 | [../runtime/05-event-streaming.md](../runtime/05-event-streaming.md) |
| StreamBridge 设计决策 | [../runtime/09-design-decisions.md](../runtime/09-design-decisions.md) |
| 文件上传全流程 | [../../../docs/lifecycle/06-file-upload.md](../../../docs/lifecycle/06-file-upload.md) |
| 消息分组与渲染 | [01-rendering-logic.md](01-rendering-logic.md) 第三节 |
