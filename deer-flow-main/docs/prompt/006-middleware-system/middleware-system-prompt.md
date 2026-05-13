# 006-中间件系统

## 解决什么问题

Agent 的 LLM 调用和工具执行之间需要插入横切逻辑：循环检测、错误处理、记忆持久化、工具过滤、子代理限流等。
这些逻辑与业务无关、不能写在 Agent Loop 里（否则 Loop 代码膨胀到不可维护），也不能写在工具里（工具不应关心调度策略）。
中间件在调用链的固定钩子点（before/after/wrap）拦截，让横切逻辑与业务逻辑正交。

## 本模块的职责边界

**只负责中间件实现**：每个中间件是一个独立的横切关注点。
不负责：中间件的排列顺序（Agent 工厂的事）、中间件注册机制（框架的事）、状态字段的定义（状态模式的事）。

## 不可变的设计决策

**每个中间件一个钩子点，不多占**：
- `before_agent`：ThreadDataMiddleware、UploadsMiddleware（在 Agent 执行前初始化状态）
- `after_agent`：MemoryMiddleware（在 Agent 执行后持久化记忆）
- `before_model`：ViewImageMiddleware（在 LLM 调用前注入图片）
- `after_model`：LoopDetectionMiddleware、TitleMiddleware、TokenUsageMiddleware、SubagentLimitMiddleware（在 LLM 响应后处理）
- `wrap_tool_call`：ToolErrorHandlingMiddleware、ClarificationMiddleware（拦截工具执行）
- `wrap_model_call`：DanglingToolCallMiddleware、DeferredToolFilterMiddleware（拦截模型调用）

每个中间件只声明它需要的钩子，不空实现其他钩子。

**LoopDetection 两级阈值**：warn(3次) 注入 HumanMessage 提醒；hard(5次) 剥离 tool_calls 强制停止。
不用单一阈值——一次警告给 LLM 自我纠正的机会，直接剥离则太粗暴。

**LoopDetection 注入 HumanMessage 而非 SystemMessage**：Anthropic API 要求 system 消息仅在对话开头出现，中途注入会导致 `_format_messages` 崩溃。HumanMessage 适用于所有提供商。

**LoopDetection 哈希与顺序无关**：`_hash_tool_calls` 对工具调用排序后哈希。相同工具调用集的不同排列产生相同哈希——LLM 并行调用的顺序不可预测。

**ToolErrorHandling 吞异常返回 ToolMessage**：`wrap_tool_call` 的 try/except 把异常转为 `ToolMessage(status="error")`，不向上抛出。LLM 收到错误信息后可以换一个工具或调整参数。`GraphBubbleUp` 必须放行——它是 LangGraph 的控制流信号（中断/暂停），吞掉会破坏人机交互。

**ToolErrorHandling 截断错误消息到 500 字**：某些异常堆栈极长，原样传递给 LLM 浪费 token。截断后保留类名和前 500 字符。

**DanglingToolCall 用 wrap_model_call 而非 before_model**：用户中断后消息历史中出现"有 tool_calls 但无对应 ToolMessage"的 AIMessage。`wrap_model_call` 修改 `request.messages`，补丁插入到悬空 AIMessage 之后（正确位置）。`before_model` 返回的消息追加到末尾，位置错误。

**DeferredToolFilter 在 bind_tools 前移除延迟工具**：MCP 工具注册到 DeferredToolRegistry 后，ToolNode 持有全部工具用于执行路由，但 `request.tools` 中移除延迟工具的 schema——LLM 只看到活跃工具，节省上下文 token。

**SubagentLimit 截断而非拒绝**：LLM 一次生成 5 个 task 调用时，只保留前 N 个，丢弃多余的。比提示词限制更可靠——LLM 经常忽略"最多并发 3 个"的指令。

**SubagentLimit 钳制到 [2,4]**：`_clamp_subagent_limit` 强制范围。低于 2 无法并行，高于 4 资源消耗过大。

**Memory 过滤中间步骤**：`_filter_messages_for_memory` 只保留用户输入和最终响应，丢弃工具消息和带 tool_calls 的 AI 消息。同时剥离 `<uploaded_files>` 块——文件路径是会话范围的不应持久化到长期记忆。

**运行时中间件分 lead/subagent 两套**：`build_lead_runtime_middlewares` 包含 Uploads + DanglingToolCall；`build_subagent_runtime_middlewares` 不包含。子代理不需要上传文件处理，也不需要悬空修复（子代理不会中途中断）。

**ClarificationMiddleware 必须在链尾**：`wrap_tool_call` 拦截 `ask_clarification` 后返回 `Command(goto=END)` 中断执行。如果后面还有中间件处理这个工具调用，中断信号会被覆盖。

## 适配层

```yaml
<ADAPT>
# === 框架 ===
middleware_base: "AgentMiddleware[State]"
hooks:
  - "before_agent(state, runtime) -> dict | None"
  - "after_agent(state, runtime) -> dict | None"
  - "before_model(state, runtime) -> dict | None"
  - "after_model(state, runtime) -> dict | None"
  - "wrap_tool_call(request, handler) -> ToolMessage | Command"
  - "wrap_model_call(request, handler) -> ModelResponse"

# === 中间件列表（按需启用）===
middlewares:
  safety:
    - name: "LoopDetectionMiddleware"
      hooks: ["after_model"]
      params: { warn_threshold: 3, hard_limit: 5, window_size: 20 }
    - name: "ToolErrorHandlingMiddleware"
      hooks: ["wrap_tool_call"]
    - name: "DanglingToolCallMiddleware"
      hooks: ["wrap_model_call"]
      scope: "lead_only"

  features:
    - name: "TitleMiddleware"
      hooks: ["after_model"]
    - name: "TokenUsageMiddleware"
      hooks: ["after_model"]
    - name: "SubagentLimitMiddleware"
      hooks: ["after_model"]
      params: { max_concurrent: 3 }
      scope: "when_subagent_enabled"
    - name: "ClarificationMiddleware"
      hooks: ["wrap_tool_call"]
      position: "terminal"

  memory:
    - name: "MemoryMiddleware"
      hooks: ["after_agent"]
    - name: "UploadsMiddleware"
      hooks: ["before_agent"]
      scope: "lead_only"
    - name: "ViewImageMiddleware"
      hooks: ["before_model"]
      scope: "when_vision_enabled"

  tool_deferral:
    - name: "DeferredToolFilterMiddleware"
      hooks: ["wrap_model_call"]
      scope: "when_tool_search_enabled"
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | 相同工具调用 3 次 | 注入 HumanMessage 警告 |
| 2 | 相同工具调用 5 次 | 剥离 tool_calls + 强制停止 |
| 3 | 相同工具调用 3 次 + 4 次 | 第二次不再重复警告 |
| 4 | 工具抛异常 | 返回 ToolMessage(status="error")，Agent 继续运行 |
| 5 | 工具抛 GraphBubbleUp | 向上放行，不吞 |
| 6 | 消息历史有悬空 tool_call | 注入占位 ToolMessage |
| 7 | 延迟工具在 request.tools 中 | 被 DeferredToolFilter 移除 |
| 8 | tool_search promote 后 | 后续 bind_tools 不再过滤 |
| 9 | LLM 生成 5 个 task 调用 | 保留前 3 个，丢弃 2 个 |
| 10 | max_concurrent=1 | 钳制到 2（不低于下限） |
| 11 | ask_clarification 被拦截 | Command(goto=END)，执行中断 |
| 12 | 非 clarification 工具 | 正常执行 handler |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **状态模式** | ThreadState / 各中间件最小状态子集 |
| **配置系统** | `get_memory_config()` / `get_title_config()` |
| **模型工厂** | `create_chat_model()` (TitleMiddleware) |
| **工具系统** | `DeferredToolRegistry` (DeferredToolFilter) |
| **记忆系统** | `get_memory_queue()` (MemoryMiddleware) |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `tool_error_handling_middleware.py` | 工具错误 → ToolMessage | `wrap_tool_call` 的 try/except 模式；`GraphBubbleUp` 放行；500 字截断；`_build_runtime_middlewares` 分 lead/subagent 两套 |
| `loop_detection_middleware.py` | 循环检测 | `_hash_tool_calls` 顺序无关哈希；两级阈值 warn/hard；HumanMessage 而非 SystemMessage；OrderedDict LRU 驱逐 |
| `dangling_tool_call_middleware.py` | 悬空工具调用修复 | `wrap_model_call` 修改 request.messages（不是 before_model 追加）；按 tool_call_id 匹配；占位 ToolMessage |
| `deferred_tool_filter_middleware.py` | 延迟工具过滤 | `wrap_model_call` 从 request.tools 移除延迟工具；ToolNode 仍持有全部工具执行路由 |
| `subagent_limit_middleware.py` | 子代理并发限流 | `after_model` 截断 task tool_calls；`_clamp_subagent_limit` [2,4] 范围钳制 |
| `token_usage_middleware.py` | Token 用量日志 | `after_model` 读取 `usage_metadata`；纯日志无状态变更 |
| `memory_middleware.py` | 记忆持久化 | `after_agent` 过滤中间步骤 + 排队异步更新；`_filter_messages_for_memory` 剥离工具调用和上传块 |
| `clarification_middleware.py` | 澄清拦截 | `wrap_tool_call` 拦截 ask_clarification；`Command(goto=END)` 中断执行 |

源码文件见同目录下的 `src/` 子文件夹。
