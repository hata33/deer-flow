# 06 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **中间件链而非继承** | 横切关注点分离，组合优于继承 |
| 2 | **固定中间件顺序** | 依赖关系决定顺序，ClarificationMiddleware 必须最后 |
| 3 | **使用 LangGraph 的 create_react_agent** | 成熟的 ReAct 实现，避免重复造轮子 |
| 4 | **静态系统提示词 + ID-swap 动态注入** | 最大化 prefix-cache 复用率 |
| 5 | **ToolErrorHandling 将异常转为 ToolMessage** | 让 Agent 自主决策下一步，而非崩溃终止 |
| 6 | **ClarificationMiddleware 使用 Command(goto=END)** | 澄清是用户交互行为，需中断图执行等待回复 |
| 7 | **LoopDetectionMiddleware 强制文本回答** | 安全机制，防止无限循环消耗资源 |

---

## 二、逐决策分析

### 决策 1：中间件链 vs 类继承

**问题**：Agent 有 ~20 个横切关注点（记忆、摘要、错误处理、循环检测等），如何组织？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 中间件链（当前） | 每个关注点独立，可按需启用/禁用；顺序灵活调整 | 需要维护固定顺序 |
| 类继承层次 | 类型安全，IDE 友好 | 菱形继承问题；添加新关注点需修改继承链 |

**选择中间件链**：`AgentMiddleware` 的五个钩子（`before_agent`/`wrap_model_call`/`after_model`/`wrap_tool_call`/`after_agent`）覆盖了 Agent 生命周期的所有阶段。每个中间件只需实现自己关心的钩子，不关心其他中间件的存在。`_build_middlewares()` 通过配置驱动按需组装——`is_plan_mode` 控制 TodoMiddleware、`supports_vision` 控制 ViewImageMiddleware、`subagent_enabled` 控制 SubagentLimitMiddleware。

---

### 决策 2：固定中间件顺序

**问题**：为什么中间件顺序是硬编码的而非动态推导？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 硬编码顺序（当前） | 依赖关系明确可审计；不会因配置错误导致运行时异常 | 添加新中间件需理解全链路 |
| 动态推导（依赖图） | 灵活 | 循环依赖检测复杂；排序结果不可预测 |

**关键依赖约束**：
- **ThreadDataMiddleware 必须在 SandboxMiddleware 之前**：沙箱初始化需要 thread_id 对应的目录已创建
- **ToolErrorHandlingMiddleware 在业务中间件之前**：所有工具异常统一兜底
- **DynamicContextMiddleware 在 SummarizationMiddleware 之后**：摘要压缩后注入记忆，避免记忆被摘要
- **ClarificationMiddleware 必须最后**：如果其他中间件在澄清后处理，会干扰用户交互流程

`_build_middlewares()` 通过严格的 `append` 顺序保证这些约束。自定义中间件通过 `custom_middlewares` 参数插入，仅在 ClarificationMiddleware 之前。

---

### 决策 3：LangGraph 的 create_react_agent vs 手写 ReAct

**问题**：为什么不在 Agent 层自己实现 ReAct 循环？

| 方案 | 优势 | 劣势 |
|------|------|------|
| `create_react_agent`（当前） | 成熟的 ReAct 实现；自动处理 tool_calls 路由、消息配对 | 黑盒，定制受限于中间件机制 |
| 手写 ReAct | 完全控制循环逻辑 | 需处理流式、并行工具调用、错误恢复等边界情况 |

**选择 `create_react_agent`**：LangGraph 的 `create_agent()` 内部封装了完整的 ReAct 循环——model call → tool call → model call → ... → 最终文本回答。DeerFlow 通过中间件链（而非修改循环本身）注入所有横切逻辑。这避免了重新实现流式响应、并行工具调用、消息配对验证等复杂度。

---

### 决策 4：静态系统提示词 + ID-swap 技术

**问题**：每用户不同的记忆数据如何注入系统提示词？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 静态 prompt + 运行时注入（当前） | prefix-cache 跨用户复用；节省 30-50% 首token延迟 | 注入逻辑在中间件中，调试路径更长 |
| 动态 prompt（含 `{memory}` 占位符） | 简单直接 | 每用户每会话不同的 system prompt，cache 命中率为零 |

**ID-swap 技术的实现**：`DynamicContextMiddleware._make_reminder_and_user_messages()` 将原始 HumanMessage 的 ID 分配给 reminder 消息（记忆+日期），原始用户消息用 `{id}__user` 作为新 ID。LangGraph 的 `add_messages` reducer 通过 ID 匹配原位替换，用户消息紧随其后。reminder 消息标记 `hide_from_ui=True`，前端不显示。

**为什么记忆不在 system prompt 里**：`apply_prompt_template()` 构建的 `SYSTEM_PROMPT_TEMPLATE` 不含 `{memory}` 占位符。相同 agent_name + 相同 skills 的系统提示词在所有用户、所有会话中完全一致，最大化 LLM 提供商的前缀缓存命中率。

---

### 决策 5：ToolErrorHandling 将异常转为 ToolMessage

**问题**：工具执行抛出异常时，Agent 应该崩溃还是继续？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 转 ToolMessage（当前） | Agent 可看到错误并自主决策替代方案 | Agent 可能重试同一失败工具 |
| 让异常传播 | 简单，终止运行 | 整个对话崩溃，用户需要重新开始 |

**选择转 ToolMessage**：`ToolErrorHandlingMiddleware.wrap_tool_call()` 捕获所有非 `GraphBubbleUp` 异常，构造 `ToolMessage(status="error")` 返回给 Agent。错误消息包含工具名、异常类型和截断后的详情（500 字符上限），末尾附加 "Continue with available context, or choose an alternative tool" 引导 Agent 选择替代方案。

**GraphBubbleUp 例外**：LangGraph 的 interrupt/pause/resume 控制流信号通过 `GraphBubbleUp` 异常传递。如果被捕获转 ToolMessage，工作流暂停机制会失效。因此必须 `raise` 透传。

---

### 决策 6：ClarificationMiddleware 使用 Command(goto=END) 中断

**问题**：用户澄清应该作为普通工具调用执行，还是中断图执行？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 工具调用执行 | 简单，不破坏 ReAct 循环 | Agent 可能继续执行而非等待用户回复 |
| Command(goto=END) 中断（当前） | 确保执行暂停，等待用户回复 | 图执行中断，需要 LangGraph 的 interrupt 机制支持恢复 |

**选择 Command(goto=END)**：澄清是 Agent 与用户的交互行为，不是工具执行。`_handle_clarification()` 构建 `ToolMessage`（包含格式化后的问题），通过 `Command(update={messages: [tool_message]}, goto=END)` 返回。LangGraph 将消息持久化后中断图执行。用户回复时，LangGraph 自动恢复执行，Agent 看到用户回复后继续工作。

**消息 ID 稳定性**：`_stable_message_id()` 使用 `clarification:{tool_call_id}` 生成确定性 ID。重试的澄清调用替换而非追加消息，避免历史中出现重复问题。

---

### 决策 7：LoopDetectionMiddleware 强制文本回答

**问题**：Agent 无限循环调用同一工具怎么办？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 达到递归限制终止 | 简单 | 用户看到原始异常信息，不友好 |
| 强制文本回答（当前） | Agent 输出有意义的总结；用户得到部分结果 | 可能丢失部分未完成的工具调用结果 |

**双层检测策略**：
1. **哈希检测**：对工具调用集合（name + args）计算哈希，在滑动窗口（默认 20）中跟踪。相同哈希出现 >= 3 次注入警告，>= 5 次强制停止。
2. **频率检测**：追踪同一工具类型的调用次数（不限参数），捕获哈希检测遗漏的跨文件读循环。

**强制停止机制**：`_apply()` 在 hard_stop 时用 `model_copy(update=...)` 替换最后的 AIMessage——清空 `tool_calls`，清除 `additional_kwargs` 中的 tool_calls 元数据，将 `finish_reason` 从 "tool_calls" 改为 "stop"。这确保 Agent 不会尝试执行已剥离的工具调用，而是输出文本回答。

**已知限制（v2.0-m1 WORKAROUND）**：警告消息被追加到 AIMessage.content 而非注入独立 HumanMessage，因为在 `after_model` 时工具调用尚未执行，插入非工具消息会破坏 OpenAI/Moonshot 严格配对验证。正确修复见 RFC #2517。
