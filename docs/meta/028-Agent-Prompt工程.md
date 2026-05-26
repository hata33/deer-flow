# Agent Prompt 工程

**问题**: Agent 的系统提示是影响质量的唯一最大因素。写不好 prompt，再强的模型也白搭。写好了 prompt，普通模型也能胜任复杂任务。

---

## 问题 1：Agent 的系统提示由哪几部分组成？

```
SYSTEM_PROMPT（静态模板）
    ├── <role> — 身份定义和核心职责
    ├── {soul} — 自定义人格（SOUL.md）
    ├── <thinking_style> — 思考方式指引
    ├── {skills_section} — 可用技能列表
    ├── {deferred_tools_section} — 延迟加载工具
    ├── {subagent_section} — 子 Agent 调度指令
    └── <guidelines> — 行为规范和约束

+ 运行时动态注入（DynamicContextMiddleware）
    ├── 记忆（2000 token 预算）
    ├── 当前日期
    └── 用户上下文
```

关键设计：**静态部分完全固定**，动态内容通过 `<system-reminder>` 注入到第一条 HumanMessage，不修改系统提示本身。

---

## 问题 2：为什么系统提示要静态？

**Prefix Cache 优化**：

```
用户 A: 系统提示(10k, 固定) + 动态注入(200 token) → LLM
用户 B: 系统提示(10k, 相同!) + 动态注入(300 token) → LLM
用户 C: 系统提示(10k, 相同!) + 动态注入(150 token) → LLM
```

系统提示完全相同 → LLM Provider 的前缀缓存命中 → 第一个 token 延迟降低 30-50%，输入成本降低 ~50%。

如果把用户信息塞进系统提示，每个用户的提示都不一样，缓存全部失效。

---

## 问题 3：技能怎么在 Prompt 中呈现？

**渐进式加载**——不是把技能全文塞进去：

```
系统提示中只列出:
┌─────────────────────────────────┐
│ 可用技能:                       │
│ 1. react-dev: React 前端开发     │
│    路径: /mnt/skills/react-dev/  │
│ 2. python-ds: Python 数据科学   │
│    路径: /mnt/skills/python-ds/  │
└─────────────────────────────────┘

Agent 判断需要 react-dev → 调用 read_file("/mnt/skills/react-dev/SKILL.md")
→ 获得完整技能指令 → 按需执行
```

为什么：10 个技能全文可能 2 万 token，全部塞入浪费。只列名字和路径，按需加载。

---

## 问题 4：思考方式（thinking_style）怎么引导？

```xml
<thinking_style>
- Think concisely and strategically
- Break complex problems into steps
- Before acting, consider: What do I know? What do I need? What could go wrong?
- When uncertain, prefer clarification over guessing
</thinking_style>
```

这不是 Chain-of-Thought（CoT），而是**元认知引导**——教 Agent "怎么思考"，不教"思考什么"。让模型自己推理具体步骤。

---

## 问题 5：动态上下文怎么注入而不破坏缓存？

通过 `<system-reminder>` 标签注入到 HumanMessage：

```xml
<system-reminder>
当前日期: 2026-05-26
用户记忆:
  工作上下文: 后端工程师，负责支付系统
  偏好: TypeScript, 暗色主题
  当前关注: 数据库迁移到 PostgreSQL
</system-reminder>

用户实际消息: 帮我重构支付模块
```

系统提示完全不变（缓存命中），动态内容放在用户消息中。LLM 看到的效果一样，但成本和延迟更低。

---

## 问题 6：CLARIFY → PLAN → ACT 工作流怎么约束？

在系统提示中用严格优先级：

```
优先级（从高到低）:
1. CLARIFY — 信息不足时必须先澄清
2. PLAN    — 复杂任务先制定计划
3. ACT     — 执行具体操作

禁止:
- 在信息不足时直接行动
- 跳过计划直接编码
- 不确认风险就执行危险操作
```

五种必须澄清的场景：缺少信息、需求模糊、方案选择、风险确认、主动建议。

---

## 问题 7：子 Agent 调度指令怎么写？

系统提示中告诉 Agent 何时、如何委派：

```
任务委派规则:
- 预计超过 5 步的任务 → 考虑使用 task 工具委派
- 独立可并行的子任务 → 同时创建多个 task
- 最多同时 3 个子 Agent
- 超过 3 个 → 分批执行
- 每批完成后综合结果，再决定下一批
```

Agent 不需要知道子 Agent 的内部实现——只管"要不要委派"和"怎么分批"。

---

## 问题 8：引用（Citation）怎么强制？

搜索工具的结果必须引用来源：

```
规范:
- 使用外部信息时必须标注来源
- 格式: [citation:标题](URL)
- 回复末尾附加完整来源列表
- 不得编造不存在的来源
```

搜索工具返回标准化的 JSON 结构，Agent 据此生成引用：

```json
{
  "title": "React 19 新特性",
  "url": "https://react.dev/blog/...",
  "snippet": "React 19 引入了..."
}
```

---

## 问题 9：结构化输出怎么引导？

通过系统提示中的格式要求：

```
输出规范:
- 代码变更: 使用 ```language 代码块
- 文件操作: 先列路径，再列内容
- 分析报告: 使用 Markdown 标题和列表
- 不确定时: 明确标注 [不确定]
```

不使用 JSON Schema 强制输出——因为 Agent 的输出应该是自然语言，不是结构化数据。只在工具参数层面做结构化约束。

---

## 问题 10：Prompt 的测试和迭代怎么做？

| 测试类型 | 测什么 | 方法 |
|---------|--------|------|
| 组件测试 | 各段落是否按条件包含 | Mock 配置，检查输出 |
| 记忆注入 | 置信度排序、token 截断 | 构造记忆数据，验证格式 |
| 技能加载 | 渐进式加载逻辑 | Mock 文件系统 |
| 集成测试 | 端到端对话质量 | 人工评估 + 自动评分 |

```python
# 测试示例: 验证记忆注入按置信度排序
def test_format_memory_sorts_facts_by_confidence():
    facts = [
        {"content": "偏好A", "confidence": 0.7},
        {"content": "偏好B", "confidence": 0.95},
        {"content": "偏好C", "confidence": 0.8},
    ]
    result = format_memory_for_injection({"facts": facts})
    # 验证: 偏好B(0.95) 排在 偏好C(0.8) 前面
    assert result.index("偏好B") < result.index("偏好C")
```

---

## 数据流概览

```
系统提示构建:
    │
    ├── 静态模板（固定，不变）
    │   ├── 身份定义
    │   ├── 思考方式
    │   ├── 技能列表（名字 + 路径）
    │   └── 行为规范
    │
    └── 运行时注入（每次不同）
        ├── 记忆（2000 token 预算，置信度排序）
        ├── 当前日期
        └── 用户上下文

LLM 看到的:
    SystemMessage: [10k token, 固定, 缓存命中]
    HumanMessage:  [200-300 token, 包含动态注入]
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 系统提示模板 | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` |
| Agent 构建 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` |
| 动态上下文注入 | `backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py` |
| 记忆格式化 | `backend/packages/harness/deerflow/agents/memory/prompt.py` |
| Prompt 测试 | `backend/tests/test_lead_agent_prompt.py` |

## 深入阅读

- [Agent 设计决策](../core/agent/06-design-decisions.md) — Prompt 设计选择
- [Agent 特性与策略](../core/agent/04-features-and-strategies.md) — 推理策略
- [记忆系统](004-记忆系统.md) — 记忆注入机制
- [技能加载](006-技能加载链路.md) — 渐进式技能加载
