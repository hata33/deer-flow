# 多Agent协作模式

**问题**: 单个 Agent 处理复杂任务时，能力、专注度和可靠性都有上限。多 Agent 协作可以分工、并行、互相校验，但引入了编排复杂度、通信开销和一致性挑战。

---

## 问题 1：多 Agent 有哪些协作模式？

| 模式 | 结构 | 适用场景 | 复杂度 |
|------|------|---------|-------|
| **主从（Orchestrator-Worker）** | 主 Agent 派任务给子 Agent | DeerFlow 默认模式 | 低 |
| **管道（Pipeline）** | A→B→C→D 顺序处理 | 流水线式任务 | 中 |
| **辩论（Debate）** | 多 Agent 讨论→投票决策 | 需要多角度分析 | 高 |
| **层级（Hierarchical）** | 总管→组长→组员 | 大型项目 | 高 |
| **对等（Peer）** | Agent 间直接通信 | 分布式问题 | 极高 |

DeerFlow 使用**主从模式**，复杂度最低，适合大多数场景。

---

## 问题 2：DeerFlow 的主从模式怎么工作？

```
主 Agent（Lead Agent）
    │
    ├── 分析任务 → 决定是否需要委派
    │
    ├── 简单任务 → 自己处理
    │
    └── 复杂任务 → 拆分为子任务
        ├── task("研究 React 19", agent="research")
        ├── task("写单元测试", agent="general-purpose")
        └── task("执行测试", agent="bash")
```

关键约束：
- **单层委派**：子 Agent 不能再创建子 Agent（工具剥夺）
- **并发限制**：最多 3 个子 Agent 同时运行
- **超时兜底**：每个子 Agent 15 分钟超时

---

## 问题 3：为什么选择主从而不是层级？

| 维度 | 主从 | 层级 |
|------|------|------|
| Token 消耗 | 低（1 层通信） | 高（多层通信，每层都有系统提示） |
| 调试难度 | 低（只有主-子两层） | 高（追踪多层调用链） |
| 错误传播 | 局限在子 Agent | 可能级联到上层 |
| 灵活性 | 足够覆盖大多数场景 | 更灵活但更复杂 |
| 实现 | 简单 | 复杂 |

经验：**大多数场景用不到层级模式**。主从 + 好的 Prompt 已经能解决 95% 的问题。

---

## 问题 4：任务怎么拆分和分配？

主 Agent 的拆分策略（由系统提示引导）：

```
收到任务: "开发一个 REST API 并部署"

Agent 拆分:
├── 子任务 1: 设计 API schema（并行）
├── 子任务 2: 搭建项目结构（并行）
├── 子任务 3: 实现业务逻辑（依赖 1、2）
├── 子任务 4: 编写测试（依赖 3）
└── 子任务 5: 部署（依赖 4）

分批执行:
批次 1: [子任务 1, 子任务 2]（并行，≤3 个）
批次 2: [子任务 3]（依赖批次 1 完成）
批次 3: [子任务 4]（依赖批次 2 完成）
批次 4: [子任务 5]（依赖批次 3 完成）
```

Agent 自主判断依赖关系和并行策略——不是预设的 DAG。

---

## 问题 5：子 Agent 之间能通信吗？

**不能**。子 Agent 之间完全隔离：

```
主 Agent → task("任务 A") → 子 Agent A
主 Agent → task("任务 B") → 子 Agent B

子 Agent A 看不到子 Agent B 的执行过程
子 Agent B 看不到子 Agent A 的执行过程
```

好处：
- **简单**：不需要通信协议
- **安全**：子 Agent 之间不会互相干扰
- **可调试**：每个子 Agent 独立追踪

代价：子 Agent 不能直接协作。如果需要协作，必须通过主 Agent 中转。

---

## 问题 6：子 Agent 的结果怎么合并？

主 Agent 收到所有子 Agent 的结果后，自己做综合分析：

```
子 Agent A: "React 19 引入了 Server Components、Actions..."
子 Agent B: "Vue 3.5 改进了响应式系统、新增 useTemplateRef..."

主 Agent:
Thought: "我收到了 React 和 Vue 的研究结果。
         现在需要综合分析，给用户一个对比表格。"
→ 生成最终输出（不需要额外 LLM 调用来"合并"）
```

合并是主 Agent 的推理过程，不是额外的系统组件。

---

## 问题 7：管道模式怎么做？

通过主 Agent 串联子任务：

```
主 Agent 编排:
    │
    ▼ 步骤 1
task("分析需求文档", agent="research")
    │
    ▼ 结果传递给步骤 2
task("根据分析结果生成代码", agent="general-purpose")
    │
    ▼ 结果传递给步骤 3
task("运行测试验证", agent="bash")
```

虽然是管道，但每步都是独立的子 Agent 执行。中间结果通过主 Agent 传递。

---

## 问题 8：辩论模式有什么用？

DeerFlow 没有内置辩论模式，但可以用子 Agent 模拟：

```
主 Agent:
    │
    ▼ 方案 A
task("提出重构方案 A：微服务架构", agent="general-purpose")
    │
    ▼ 方案 B
task("提出重构方案 B：模块化单体", agent="general-purpose")
    │
    ▼ 评估
"综合两个方案的优缺点:
  方案 A 优点: ... 缺点: ...
  方案 B 优点: ... 缺点: ...
  推荐方案 B，因为..."
```

两个子 Agent 独立思考，主 Agent 做裁判。

---

## 问题 9：多 Agent 的成本怎么控制？

| 措施 | 效果 |
|------|------|
| 子 Agent 用便宜模型 | 成本降低 ~80% |
| 并发限制（最多 3 个） | 防止成本倍增 |
| 超时机制（15 分钟） | 防止无限运行 |
| 工具剥夺 | 子 Agent 不做不必要的事 |
| Token 归集 | 可追踪每个子 Agent 的消耗 |

```
成本对比:
单 Agent:     1 个强模型 × 10 轮 = ~20,000 token
主从模式: 1 个强模型 × 3 轮 + 3 个弱模型 × 5 轮
             = 6,000 + 7,500 = ~13,500 token (省 32%)
```

---

## 问题 10：多 Agent 的调试怎么做？

| 问题 | 调试方法 |
|------|---------|
| 子 Agent 没有完成任务 | 查看 StreamBridge 中的子 Agent 事件流 |
| 主 Agent 拆分不合理 | 查看主 Agent 的 Thought（LLM 推理过程） |
| 子 Agent 超时 | 查看 RunJournal 中的事件时间线 |
| 结果合并不好 | 查看主 Agent 收到子 Agent 结果后的 Thought |

```
调试链路:
LangSmith Trace → 找到主 Agent 的 tool_call
    └→ 展开 task 工具调用 → 看到子 Agent 的完整 Trace
        └→ 展开子 Agent 的每个工具调用
```

---

## 数据流概览

```
用户: "重构整个项目"
    │
    ▼ 主 Agent 分析
Thought: "任务复杂，需要拆分"
    │
    ▼ 创建子任务
├── task("分析现有架构")     → 子 Agent 1 (research)
├── task("重构核心模块")     → 子 Agent 2 (general-purpose)
└── task("运行测试验证")     → 等待子 Agent 2 完成后执行
    │
    ▼ 子 Agent 并发执行（≤3）
    ├── 子 Agent 1 完成 → 返回分析结果
    └── 子 Agent 2 完成 → 返回重构结果
    │
    ▼ 主 Agent 合并
    ├── 传给子 Agent 3 执行测试
    │
    ▼ 最终输出
"重构完成，以下是变更摘要..."
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 子 Agent 注册 | `backend/packages/harness/deerflow/subagents/registry.py` |
| 子 Agent 执行 | `backend/packages/harness/deerflow/subagents/executor.py` |
| task 工具 | `backend/packages/harness/deerflow/tools/builtins/task_tool.py` |
| 并发限制 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` |

## 深入阅读

- [子 Agent 调度](003-子Agent调度系统.md) — 调度机制详解
- [Agent 推理模式](030-Agent推理模式.md) — 主从模式的推理策略
- [Agent 成本控制](024-Agent成本控制与Token管理.md) — 多 Agent 成本优化
