# 006-中间件系统模块

> 已验证来源：deer-flow 项目 `agents/middlewares/` 目录下 15 个中间件实现
> 本提示词可在新项目中直接使用，通过适配层启用/禁用中间件，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

Agent 的 LLM 调用和工具执行之间需要插入横切逻辑（循环检测、错误处理、记忆持久化、工具过滤等）。
这些逻辑与业务无关，不能写在 Agent Loop 里（否则膨胀到不可维护），也不能写在工具里（工具不应关心调度策略）。
中间件在调用链的固定钩子点拦截，让横切逻辑与业务逻辑正交。

**解决的核心痛点：**
- LLM 陷入死循环 → 两级阈值检测 + 强制停止
- 工具异常导致 Agent 崩溃 → 吞异常返回错误 ToolMessage
- 用户中断后消息格式破损 → 悬空 tool_call 补丁
- MCP 工具过多消耗 token → 延迟工具过滤
- 子代理过度并发 → 截断 task 调用
- 澄清请求混在工具流程中 → 拦截并中断执行

---

## 二、输入契约

每个中间件通过框架提供的钩子接口拦截：

| 钩子 | 时机 | 使用者 |
|------|------|--------|
| `before_agent` | Agent 执行前 | ThreadData、Uploads |
| `after_agent` | Agent 执行后 | Memory |
| `before_model` | LLM 调用前 | ViewImage |
| `after_model` | LLM 响应后 | LoopDetection、Title、TokenUsage、SubagentLimit |
| `wrap_tool_call` | 工具执行前后 | ToolErrorHandling、Clarification |
| `wrap_model_call` | 模型调用前后 | DanglingToolCall、DeferredToolFilter |

---

## 三、输出契约

### 中间件返回值语义

| 返回值 | 含义 |
|--------|------|
| `None` | 无状态变更，继续 |
| `dict` | 状态更新，合并到当前 state |
| `ToolMessage` | 工具执行结果（wrap_tool_call） |
| `Command` | 控制流指令（goto=END 中断） |

### 保证

| 保证项 | 说明 |
|--------|------|
| 工具异常不崩溃 | 转为错误 ToolMessage，Agent 继续运行 |
| 循环被终止 | 两级阈值，先警告后强制 |
| 悬空消息被修复 | 补丁插入到正确位置 |
| 延迟工具对 LLM 不可见 | bind_tools 前移除 schema |
| 澄清中断执行 | Command(goto=END) |

---

## 四、行为约束

### 约束 1：每个中间件只占一个钩子

不多占——空实现其他钩子增加不必要的调用开销。

### 约束 2：wrap_tool_call 必须放行 GraphBubbleUp

LangGraph 的控制流信号（中断/暂停/恢复）不能被吞掉。

### 约束 3：ClarificationMiddleware 必须在链尾

后面还有中间件会覆盖 goto=END 的中断信号。Agent 工厂的 `_build_middlewares` 必须强制归位。

### 约束 4：警告注入 HumanMessage 而非 SystemMessage

Anthropic 不允许中途插入 system 消息。HumanMessage 适用于所有提供商。

### 约束 5：DanglingToolCall 用 wrap_model_call

`before_model` 返回的消息追加到末尾，位置错误。`wrap_model_call` 修改 request.messages，补丁在正确位置。

### 约束 6：运行时中间件分 lead/subagent 两套

子代理不需要 Uploads（无上传）和 DanglingToolCall（不会中途中断）。

---

## 五、验证场景

| # | Given | When | Then |
|---|-------|------|------|
| 1 | 相同工具调用 3 次 | after_model | 注入 HumanMessage 警告 |
| 2 | 相同工具调用 5 次 | after_model | 剥离 tool_calls |
| 3 | 重复警告 | 第二次 4 次 | 抑制（已 warn 过） |
| 4 | 工具抛 Exception | wrap_tool_call | 返回 error ToolMessage |
| 5 | 工具抛 GraphBubbleUp | wrap_tool_call | 放行 raise |
| 6 | 悬空 tool_call | wrap_model_call | 注入占位 ToolMessage |
| 7 | 延迟工具在 schema 中 | wrap_model_call | 被移除 |
| 8 | LLM 生成 5 个 task | after_model | 保留前 N 个 |
| 9 | ask_clarification | wrap_tool_call | Command(goto=END) |
| 10 | 非 clarification 工具 | wrap_tool_call | 正常执行 handler |

---

## 六、自由度与禁区

### 可以改的

- 中间件列表（按项目需求增减）
- 阈值参数（warn/hard/window）
- 子代理并发上限
- 记忆过滤策略
- 错误消息格式
- 标题生成模型

### 不能改的

- **GraphBubbleUp 必须放行**：吞掉破坏控制流
- **ClarificationMiddleware 链尾**：被覆盖则中断失效
- **警告用 HumanMessage**：SystemMessage 在 Anthropic 崩溃
- **DanglingToolCall 用 wrap_model_call**：before_model 位置错误
- **运行时分 lead/subagent**：子代理不应有上传和悬空处理
- **工具异常吞掉不抛**：抛出则 Agent 崩溃

---

## 七、依赖的上下游模块

```
[上游] 状态模式 → ThreadState / 中间件最小状态子集
[上游] 配置系统 → memory_config / title_config
[上游] 模型工厂 → create_chat_model (TitleMiddleware)
[上游] 工具系统 → DeferredToolRegistry (DeferredToolFilter)
    ↓
[本模块] 中间件系统
    ↓
[下游] Agent 工厂 → _build_middlewares 排列顺序
```
