# 子Agent调度系统

**问题**: 主 Agent 面对复杂多步任务时，单 Agent 难以兼顾专业性和效率，需要将子任务委派给专门的子 Agent 执行。

---

## 问题 1：怎么委派子任务？

主 Agent 调用内置工具 `task`，传入子任务描述和目标 Agent 类型。系统根据描述自动匹配合适的子 Agent，或将任务派发给指定 Agent。

```python
# 主 Agent 调用示例
task(
    description="搜索最新的 React 19 特性并总结",
    agent="research"  # 可选，不指定则自动匹配
)
```

---

## 问题 2：有哪些内置子 Agent？

| Agent | 用途 | 可用工具 | 限制 |
|-------|------|---------|------|
| `general-purpose` | 通用多步任务 | 全部（除 task/clarification） | 最多 100 轮 |
| `bash` | 命令行执行专家 | bash, ls, read_file, write_file, str_replace | 最多 60 轮 |
| 自定义 Agent | 用户定义 | 按配置 | 按配置 |

---

## 问题 3：如何防止子 Agent 无限递归创建？

子 Agent 的工具列表中**不包含 `task` 工具**。`disallowed_tools` 配置确保子 Agent 无法再创建子 Agent，形成单层委派模型。

```python
GENERAL_PURPOSE_CONFIG = SubagentConfig(
    disallowed_tools=["task", "ask_clarification", "present_files"]
)
```

设计选择：单层而非多层嵌套，避免深度递归带来的 token 爆炸和调试困难。

---

## 问题 4：子 Agent 的 token 怎么算？

`token_collector` 收集子 Agent 执行期间的所有 LLM 调用 token，汇总后归入主 Agent 的 token 用量。这样用户看到的是总消耗，而非分散的数字。

```
主 Agent (10k token)
    ├── 子 Agent A (5k token)
    └── 子 Agent B (8k token)
总计: 23k token → 归入主 Agent 账户
```

---

## 问题 5：并发子 Agent 数量有限制吗？

有。`subagent_limit_middleware` 限制同时运行的子 Agent 数量：

```yaml
subagents:
  max_concurrent: 3  # 最多 3 个同时运行
```

超出限制时，新的 `task` 调用会排队等待或返回错误。

---

## 问题 6：子 Agent 超时怎么办？

每个子 Agent 有独立的超时设置：

```yaml
subagents:
  timeout_seconds: 900  # 默认 15 分钟
  custom_agents:
    research-agent:
      timeout_seconds: 300  # 研究型任务 5 分钟
```

超时后 `executor` 取消执行，子 Agent 的部分结果（如有）仍会返回给主 Agent。

---

## 问题 7：子 Agent 的结果怎么回传？

子 Agent 执行完成后，其最终输出作为工具结果返回给主 Agent。主 Agent 将其视为普通工具返回值，继续后续推理。

```
主 Agent → task("分析这段代码") → 子 Agent 开始执行
                                        │
                                        ▼ 执行完成
                               结果作为 tool response 返回
                                        │
                                        ▼
主 Agent 收到结果，继续对话
```

---

## 问题 8：子 Agent 执行期间，用户能看到进度吗？

能。通过 `StreamBridge`，子 Agent 的执行事件（工具调用、LLM 输出等）实时推送到前端。前端用不同的样式标记子 Agent 事件，用户可以看到子 Agent 正在做什么。

---

## 问题 9：如何自定义子 Agent？

在配置文件中定义：

```yaml
subagents:
  custom_agents:
    code-reviewer:
      model: "claude-sonnet-4-20250514"
      max_turns: 30
      tools: ["read_file", "bash", "web_search"]
      disallowed_tools: ["task", "write_file"]  # 只读，不能写
      system_prompt: "You are a code review expert..."
```

---

## 数据流概览

```
主 Agent 调用 task("研究 X")
    │
    ▼ task 工具接收
subagent_limit_middleware 检查并发数
    │
    ▼ 未超限
executor 创建子 Agent 实例
    │
    ▼ 子 Agent 执行
    ├── StreamBridge 推送事件到前端
    └── token_collector 收集用量
    │
    ▼ 执行完成（或超时）
结果返回主 Agent，token 归入总账
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| task 工具 | `backend/packages/harness/deerflow/tools/builtins/task_tool.py` |
| 子 Agent 注册 | `backend/packages/harness/deerflow/subagents/registry.py` |
| 子 Agent 执行器 | `backend/packages/harness/deerflow/subagents/executor.py` |
| Token 收集 | `backend/packages/harness/deerflow/subagents/token_collector.py` |
| 并发限制中间件 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` |

## 深入阅读

- [子 Agent 调度全链路](../lifecycle/05-subagent-dispatch.md) — 完整调度流程
- [子 Agent 概览](../core/subagents/00-overview.md) — 架构设计
- [子 Agent 执行器](../core/subagents/02-executor.md) — 执行模型详解
