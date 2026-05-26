# Agent 推理模式

**问题**: Agent 不能只是"调工具的 LLM"——它需要一套推理框架来决定何时思考、何时行动、何时停止。不同场景需要不同的推理策略。

---

## 问题 1：主流推理模式有哪些？

| 模式 | 流程 | 适用场景 |
|------|------|---------|
| **ReAct** | 思考→行动→观察→循环 | 通用任务（DeerFlow 默认） |
| **Plan-and-Execute** | 先制定完整计划→逐步执行 | 复杂多步任务 |
| **Reflection** | 执行→自我评估→修正→循环 | 质量敏感任务 |
| **CLARIFY→PLAN→ACT** | 澄清→规划→行动 | 信息不充分的场景 |

DeerFlow 使用 **ReAct + CLARIFY→PLAN→ACT 混合模式**。

---

## 问题 2：ReAct 模式怎么工作？

DeerFlow 基于 LangGraph 的 `create_react_agent` 实现：

```
循环:
    │
    ▼ Thought（LLM 推理）
    "用户想要重构代码，我需要先了解项目结构"
    │
    ▼ Action（工具调用）
    bash("find . -name '*.py' | head -20")
    │
    ▼ Observation（工具结果）
    "src/main.py\nsrc/utils.py\n..."
    │
    ▼ 回到 Thought
    "项目有 3 个模块，先看 main.py 的结构..."
    │
    ▼ 继续循环...
    │
    ▼ 直到 LLM 判断任务完成
    → 输出最终结果
```

关键：LLM 自主决定每一步做什么，没有硬编码的流程。

---

## 问题 3：CLARIFY→PLAN→ACT 怎么强制执行？

在系统提示中用严格优先级约束：

```
优先级（不可跳过）:

Level 1: CLARIFY
    触发条件:
    - 缺少关键信息 → ask_clarification
    - 需求模糊 → ask_clarification
    - 多种方案可选 → ask_clarification
    - 操作有风险 → ask_clarification

Level 2: PLAN（可选，复杂任务时）
    - 超过 3 步的任务 → 先列计划
    - 多文件修改 → 先列出变更清单

Level 3: ACT
    - 信息充分、计划明确 → 执行
```

**为什么强制**: 不澄清就行动是 Agent 犯错的最大原因。强制 CLARIFY 先行，把"猜"变成"问"。

---

## 问题 4：子 Agent 的推理模式是什么？

主 Agent 做决策，子 Agent 做 ReAct：

```
主 Agent:
    Thought → "这个研究任务很复杂，委派给子 Agent"
    Action → task("研究 React 19 新特性", agent="research")

子 Agent（独立 ReAct 循环）:
    Thought → "先搜索最新信息"
    Action → web_search("React 19 new features 2026")
    Observation → 搜索结果...
    Thought → "再看官方文档"
    Action → web_fetch("https://react.dev/...")
    Observation → 文档内容...
    Thought → "信息足够了，总结"
    → 返回结果给主 Agent

主 Agent:
    Observation → 子 Agent 的总结
    Thought → "基于研究结果，给用户回复"
```

两层推理：主 Agent 做编排决策，子 Agent 做具体执行。

---

## 问题 5：如何防止 Agent 过度思考？

三个刹车机制：

| 机制 | 触发条件 | 效果 |
|------|---------|------|
| `recursion_limit` | 超过 25-50 轮 ReAct 循环 | 强制终止 |
| 循环检测 | 重复相同工具调用 | 警告→强停 |
| 任务截断 | 超过 3 个并发子 Agent | 丢弃多余任务 |

```python
# LangGraph 内置限制
agent = create_react_agent(
    model=llm,
    tools=tools,
    # recursion_limit 控制最大轮数
)
```

---

## 问题 6：Agent 如何做计划（Planning）？

系统提示引导 Agent 在复杂任务时先列计划：

```
收到任务: "把项目从 JavaScript 迁移到 TypeScript"

Agent 的计划（内部推理）:
1. 分析项目结构和依赖 → bash("find . -name '*.js'")
2. 创建 tsconfig.json → write_file
3. 逐文件迁移 .js → .ts → 循环处理
4. 安装类型定义 → bash("npm install -D @types/...")
5. 运行类型检查 → bash("npx tsc --noEmit")
6. 修复类型错误 → 循环修复

然后按计划逐步执行，每步都做 Thought→Action→Observation。
```

计划不是一次性固定的——Agent 可以在执行中调整（比如发现遗漏的依赖就修改计划）。

---

## 问题 7：Reflection（自我反思）模式怎么用？

DeerFlow 的反思体现在两个地方：

**工具执行后自动评估**:

```
Action → bash("npm test")
Observation → "2 tests failed"

Thought → "两个测试失败了。
    第一个: TypeError in utils.js:42
    原因: 我改了函数签名但没更新测试
    修复方案: 更新测试中的函数调用参数
    第二个: Timeout in api.test.js
    原因: 可能是网络问题，先重跑一次"

→ 自我诊断 → 调整策略 → 重新执行
```

**循环检测触发时的强制反思**:

```
第 3 次重复 → 警告追加到 AIMessage
"检测到可能陷入循环，请尝试不同的方法"

Agent 被迫反思:
"我一直在重复同一个命令，应该换一个思路..."
```

---

## 问题 8：不同推理模式的成本对比？

| 模式 | 平均轮数 | Token 消耗 | 质量评分 | 适用场景 |
|------|---------|-----------|---------|---------|
| 纯 ReAct | 5-8 轮 | 中 | 中 | 简单查询 |
| ReAct + CLARIFY | 6-10 轮 | 中高 | 高 | 复杂任务 |
| Plan-and-Execute | 4-6 轮 | 低 | 中高 | 可预测的多步任务 |
| ReAct + Reflection | 8-15 轮 | 高 | 极高 | 质量敏感任务 |

CLARIFY 多花 1-2 轮澄清，但避免后续 5-10 轮的返工。净效果是**降本增效**。

---

## 问题 9：如何选择推理模式？

```
任务简单（1-3 步）?
    └── 是 → 纯 ReAct
    └── 否 → 信息充分?
              └── 否 → CLARIFY → 收集信息
              └── 是 → 任务可预测?
                        └── 是 → Plan-and-Execute
                        └── 否 → 任务质量敏感?
                                  └── 是 → ReAct + Reflection
                                  └── 否 → ReAct + CLARIFY
```

DeerFlow 的选择：默认 ReAct + CLARIFY，通过系统提示引导，不需要切换模式。

---

## 问题 10：推理模式和生产问题的关系？

| 生产问题 | 推理层面的原因 | 解决的推理策略 |
|---------|-------------|-------------|
| Agent 死循环 | Thought 不充分就 Action | CLARIFY 先行 |
| Token 浪费 | 一次性想太多 | 渐进式推理 |
| 质量不稳定 | 缺乏自检环节 | Reflection |
| 信息不足就行动 | 跳过 CLARIFY | 强制优先级 |
| 过度委派 | 不合理拆分任务 | Plan 先行 |

好的推理模式不是多花 token，而是**把 token 花在正确的地方**。

---

## 数据流概览

```
用户: "帮我重构支付模块"
    │
    ▼ CLARIFY 阶段
Thought: "需要确认几个关键信息"
Action: ask_clarification("支付模块包含哪些文件？")
用户回答: "src/payment/"
    │
    ▼ PLAN 阶段
Thought: "制定重构计划"
Plan: 1. 分析现有代码 2. 识别重构点 3. 逐模块修改 4. 测试验证
    │
    ▼ ACT 阶段（ReAct 循环）
Thought: "先看 payment 目录结构"
Action: bash("find src/payment/ -name '*.py'")
Observation: "pay_service.py, pay_model.py, pay_utils.py"

Thought: "看核心文件"
Action: read_file("src/payment/pay_service.py")
Observation: 文件内容...

Thought: "发现问题，需要拆分大类"
Action: write_file(...) → 逐步重构
    │
    ▼ Reflection（自检）
Thought: "改完了，跑测试验证"
Action: bash("pytest src/payment/")
Observation: "All tests passed"
    │
    ▼ 输出结果
"重构完成，以下是变更摘要..."
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| ReAct Agent | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` |
| 系统提示（推理引导） | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` |
| 澄清工具 | `backend/packages/harness/deerflow/tools/builtins/clarification_tool.py` |
| 循环检测 | `backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py` |

## 深入阅读

- [Agent 设计决策](../core/agent/06-design-decisions.md) — 推理策略选择
- [Agent Prompt 工程](028-Agent-Prompt工程.md) — 推理引导的 Prompt 实现
- [Agent 生产面试题](023-Agent生产环境面试题.md) — 推理相关面试题
- [子 Agent 调度](003-子Agent调度系统.md) — 多层推理架构
