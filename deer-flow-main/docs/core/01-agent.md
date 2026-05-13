# Agent 主体架构——底层逻辑与本质

## 一句话本质

Agent = **自定义状态 + 中间件链 + 工具集 + 提示词模板**，四者通过工厂函数按配置组装，运行时行为完全由 `RunnableConfig` 参数驱动，不需要为每种场景写不同代码。

---

## 1. 状态是 Agent 的记忆骨架

```
AgentState (LangGraph 内置)
  └─ ThreadState (DeerFlow 扩展)
       ├─ messages: list          ← 对话消息（LangGraph 内置）
       ├─ sandbox: SandboxState   ← 沙箱 ID
       ├─ thread_data: ThreadDataState ← 线程目录路径
       ├─ title: str              ← 自动生成的标题
       ├─ artifacts: list[str]    ← 产出文件路径（带去重 reducer）
       ├─ todos: list             ← 待办列表
       ├─ uploaded_files: list    ← 上传文件元数据
       └─ viewed_images: dict     ← 已查看图片（带合并 reducer）
```

**为什么用 TypedDict 而不是 Pydantic Model？** 因为 LangGraph 的状态需要支持 **reducer 模式**——当多个节点并行写入同一个字段时，需要自定义合并策略。`artifacts` 用 `merge_artifacts` 做去重合并，`viewed_images` 用 `merge_viewed_images` 做字典覆盖 + 清空语义。TypedDict + `Annotated` 是 LangGraph 的标准做法。

**核心启示**：状态设计决定了 Agent 能"记住"什么。不要把所有数据塞进 `messages`——`messages` 是对话流，`artifacts` 是产出物，`todos` 是任务计划，它们的生命周期和合并策略完全不同。用独立字段 + 自定义 reducer 做正交分离。

## 2. 中间件链是 Agent 的行为脊柱

```
请求进入
  │
  ▼
ThreadDataMiddleware      ← 创建线程目录（基础设施）
UploadsMiddleware         ← 注入上传文件（数据准备）
SandboxMiddleware         ← 获取沙箱（执行环境）
DanglingToolCallMiddleware ← 修补缺失的 ToolMessage（防御修复）
GuardrailMiddleware       ← 工具调用授权（安全门控）[可选]
SummarizationMiddleware   ← 上下文压缩（资源管理）[可选]
TodoMiddleware            ← 任务追踪（功能增强）[可选]
TokenUsageMiddleware      ← 用量统计（可观测性）[可选]
TitleMiddleware           ← 自动标题（辅助功能）
MemoryMiddleware          ← 记忆排队（异步写入）
ViewImageMiddleware       ← 图片注入（能力增强）[条件性]
DeferredToolFilterMiddleware ← 延迟工具过滤 [条件性]
SubagentLimitMiddleware   ← 并发截断（硬约束）[条件性]
LoopDetectionMiddleware   ← 循环检测（安全兜底）
[自定义中间件插入点]
ClarificationMiddleware   ← 澄清拦截（必须在最后）
  │
  ▼
响应返回
```

**顺序为什么重要？** 每一层中间件假设它前面的层已经完成了工作：
- `SandboxMiddleware` 需要 `ThreadDataMiddleware` 已经创建了线程目录
- `SummarizationMiddleware` 要在 `MemoryMiddleware` 之前压缩，否则记忆会存储未压缩的臃肿对话
- `ClarificationMiddleware` 必须在最后，因为它会中断执行（`goto=END`），后面的中间件都不会执行

**核心启示**：不要把所有逻辑写在一个大函数里。中间件模式做正交分解——每个中间件只关心一件事，通过有序组合实现复杂行为。某个能力（视觉、记忆、计划模式）可以独立开关，不影响其他功能。新增功能只需写一个中间件类，插入链中合适位置。

## 3. 工厂函数——同一个入口，无数种运行时形态

```python
def make_lead_agent(config: RunnableConfig):
    # 从 config.configurable 提取 9 个参数
    thinking_enabled = cfg.get("thinking_enabled", True)
    model_name = cfg.get("model_name")
    is_plan_mode = cfg.get("is_plan_mode", False)
    subagent_enabled = cfg.get("subagent_enabled", False)
    # ...

    # 三级模型解析：请求参数 > 智能体配置 > 全局默认
    model_name = requested_model_name or agent_model_name

    # 按参数组装中间件链、工具集、提示词
    return create_agent(
        model=create_chat_model(name=model_name, ...),
        tools=get_available_tools(model_name, groups=..., subagent_enabled=...),
        middleware=_build_middlewares(config, model_name, ...),
        system_prompt=apply_prompt_template(subagent_enabled=..., agent_name=...),
        state_schema=ThreadState,
    )
```

`RunnableConfig` 是 LangGraph 的标准运行时配置传递机制。前端发起请求时在 configurable 中传参，LangGraph Server 透传给工厂函数。同一份代码同时支持：
- **引导模式**（`is_bootstrap=True`）用精简提示词 + `setup_agent` 工具
- **自定义智能体**（通过 `agent_name` 加载独立配置、工具组、SOUL.md 人格）
- **计划模式**、**子智能体**、**思考模式** 按需组合

**核心启示**：Agent 不应该是硬编码的单体。用工厂函数 + 声明式配置，让调用方通过参数组合获得不同行为，而不是通过代码修改。前端、CLI、API 都用同一个入口创建出形态各异的 Agent。

## 4. 提示词是动态组装的——不是静态字符串

`apply_prompt_template()` 不是简单的字符串拼接。它根据运行时参数动态决定注入哪些段落：

```
SYSTEM_PROMPT_TEMPLATE (骨架)
  ├─ {agent_name}           ← 智能体名称（可自定义）
  ├─ {soul}                 ← SOUL.md 人格描述（按智能体加载）
  ├─ {memory_context}       ← <memory> 记忆段落（按 token 预算裁剪）
  ├─ {skills_section}       ← <skill_system> 技能列表（按启用状态过滤）
  ├─ {subagent_section}     ← <subagent_system> 子智能体指令（仅启用时）
  ├─ {subagent_thinking}    ← 思维引导中的子智能体分批规划提示
  ├─ {subagent_reminder}    ← 关键提醒中的并发限制强调
  ├─ {deferred_tools_section} ← 延迟工具名称列表（仅 tool_search 启用时）
  └─ {acp_section}          ← ACP 智能体工作目录说明（仅配置了 ACP 时）
```

**为什么不用一个大提示词？** 因为不同运行时形态需要不同的指令：
- 未启用子智能体时，`<subagent_system>` 整段不出现，不浪费 token
- 未配置 ACP 智能体时，ACP 路径说明不出现，不干扰 Agent
- 记忆段落根据 `<memory>` 内容动态填充，空记忆时不注入空标签

**核心启示**：提示词是代码，不是文档。用模板 + 条件段落做动态组装，让提示词精确匹配当前运行时能力。多余的指令不仅浪费 token，还会误导 LLM 尝试它不具备的能力。

## 5. 降级而非硬失败——模型能力差异的处理哲学

```
模型名称找不到 → 降级到默认模型 + warning
请求了 thinking 但模型不支持 → 自动关闭 thinking
模型不支持 vision → 不注入 ViewImageMiddleware、不添加 view_image 工具
至少有一个模型可用 → 正常运行
没有任何模型配置 → 抛出 ValueError（这是硬失败，合理）
```

**核心启示**：区分"必须满足的前置条件"和"可以优雅降级的增强功能"。Agent 必然面对多模型、多能力的现实——用户可能用 GPT-4 做主力、用 GPT-4o-mini 做摘要，两个模型的能力集完全不同。设计时让 Agent 在任何合理配置下都能工作，而不是一遇到不匹配就崩溃。
