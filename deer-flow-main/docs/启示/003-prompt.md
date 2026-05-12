# 系统提示词构建启示

> 来源：`backend/packages/harness/deerflow/agents/lead_agent/prompt.py`、`config/agents_config.py`

## 1. 模板占位符组装——提示词是拼出来的，不是写死的

`apply_prompt_template` 本质是一个**字符串模板引擎**。`SYSTEM_PROMPT_TEMPLATE` 是一个巨大的 f-string 风格模板（`{soul}`、`{memory_context}`、`{skills_section}` 等占位符），函数根据运行时参数逐一填充：

```
SYSTEM_PROMPT_TEMPLATE.format(
    agent_name    → "DeerFlow 2.0" 或自定义智能体名称
    soul          → <soul>SOUL.md 内容</soul>（可选人格文件）
    memory_context → <memory>用户记忆数据</memory>（可选）
    skills_section → <skill_system>可用技能列表</skill_system>（可选）
    deferred_tools_section → <available-deferred-tools>延迟工具名</available-deferred-tools>（可选）
    subagent_section  → <subagent_system>完整子智能体指令</subagent_system>（可选）
    subagent_reminder → 并发限制提醒（可选）
    subagent_thinking → 分解检查思维引导（可选）
    acp_section   → ACP 代理工作目录说明（可选）
)
```

每个 section 的生成函数独立：`_get_memory_context()`、`get_skills_prompt_section()`、`get_deferred_tools_prompt_section()`、`_build_subagent_section()`、`_build_acp_section()`。不启用则返回空字符串，占位符被替换为空，段落自然消失。

**Why：** 系统提示词可能跨越上千行，但并非所有段落每次都需要。子智能体指令 ~150 行、技能列表可变、记忆可能为空、延迟工具按配置开关。硬编码为单一字符串会导致无效 token 浪费。

**How to apply：** 把系统提示词当作一个**声明式模板**来设计，而非一篇固定文章。每个能力段落有独立的生成函数和开关，通过 `format()` 拼装。这样提示词的长度和内容随运行时能力动态伸缩，不浪费一个 token。

## 2. 人格-记忆-技能三层注入——从"通用 Agent"到"个性化助手"的渐进增强

提示词中三个核心段落形成递进关系：

| 层 | 来源 | 作用 | 持久性 |
|---|---|---|---|
| **Soul（灵魂）** | `agents/{name}/SOUL.md` 文件 | 定义人格、价值观、行为准则 | 静态，开发者设定 |
| **Memory（记忆）** | `memory.json` + LLM 提取 | 用户偏好、历史事实、上下文摘要 | 动态，跨会话累积 |
| **Skills（技能）** | `skills/{public,custom}/SKILL.md` | 可加载的专业工作流知识 | 半静态，用户可安装/启用 |

- **Soul** 通过 `load_agent_soul(agent_name)` 从文件读取，包裹在 `<soul>` XML 标签中注入
- **Memory** 通过 `get_memory_data(agent_name)` → `format_memory_for_injection(memory_data, max_tokens)` 读取并裁剪到 token 预算内
- **Skills** 通过 `load_skills(enabled_only=True)` 扫描目录，生成 `<available_skills>` 列表，Agent 按需 `read_file` 加载完整内容

三层从静态到动态、从全局到个性化，共同把一个通用 Agent 变成"认识用户、有专长、有性格"的助手。

**Why：** Agent 的核心价值不在于模型能力（所有 Agent 都用同样的 LLM），而在于**持久上下文**和**专业能力**。Soul 给它性格，Memory 给它记忆，Skills 给它专长。

**How to apply：** 设计 Agent 系统时，区分"一次性配置"（Soul）、"跨会话累积"（Memory）、"按需加载"（Skills）三类上下文。分别用不同的存储、注入、更新策略处理，而不是把所有东西混在一个 prompt 里。每层有独立的 token 预算控制（如 Memory 的 `max_injection_tokens`），防止单一层膨胀挤占其他层。

## 3. 并发限制的三重约束——提示词约束 × 思维引导 × 中间件截断

子智能体并发控制贯穿提示词的三处位置，形成递进式约束：

**第一层：提示词显式约束**（`_build_subagent_section`）
```
⛔ HARD CONCURRENCY LIMIT: MAXIMUM {n} `task` CALLS PER RESPONSE.
```
在 `<subagent_system>` 中反复强调限制（出现 5 次以上），用 `⛔` emoji、`HARD ERROR`、`WILL discard` 等强语气阻止 LLM 越限。

**第二层：思维引导**（`subagent_thinking`）
```
- DECOMPOSITION CHECK: Can this task be broken into 2+ parallel sub-tasks?
  If count > {n}, you MUST plan batches of ≤{n} and only launch the FIRST batch now.
```
嵌入 `<thinking_style>` 段落，在 LLM 思考阶段就引导它做计数和分批规划，而非在行动阶段才发现超限。

**第三层：中间件硬截断**（`SubagentLimitMiddleware`）
即使提示词约束和思维引导都失败了，`SubagentLimitMiddleware` 在 `after_model` 阶段会**物理截断**超出的 `task` 工具调用。LLM 无法绕过。

**Why：** LLM 不是确定性程序，仅靠提示词约束不能保证合规。需要"软约束（提示词）+ 引导（思维注入）+ 硬约束（中间件截断）"三重保障。

**How to apply：** 对 Agent 的关键行为约束（并发限制、安全边界、资源配额），永远不要只依赖提示词。设计三层防护：提示词告知规则 → 思维引导内化规则 → 中间件强制执行规则。这是 Agent 系统的"纵深防御"模式。

## 附：`apply_prompt_template` 调用链路

```
make_lead_agent(config)
    │
    ├── is_bootstrap?
    │     └── apply_prompt_template(subagent_enabled, max_concurrent_subagents, available_skills={"bootstrap"})
    │
    └── 默认模式
          └── apply_prompt_template(subagent_enabled, max_concurrent_subagents, agent_name)
                │
                ├── _get_memory_context(agent_name)
                │     └── get_memory_data() → format_memory_for_injection(memory_data, max_tokens)
                │
                ├── get_agent_soul(agent_name)
                │     └── load_agent_soul(agent_name) → 读取 SOUL.md 文件
                │
                ├── get_skills_prompt_section(available_skills)
                │     └── load_skills(enabled_only=True) → 扫描 skills/{public,custom}/
                │
                ├── get_deferred_tools_prompt_section()
                │     └── get_deferred_registry() → 延迟工具名列表
                │
                ├── _build_subagent_section(max_concurrent_subagents)
                │     └── get_available_subagent_names() → 动态子智能体类型列表
                │
                ├── _build_acp_section()
                │     └── get_acp_agents() → 外部代理配置
                │
                └── SYSTEM_PROMPT_TEMPLATE.format(...) + <current_date>
```
