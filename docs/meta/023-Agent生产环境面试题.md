# Agent 生产环境面试题

**问题**: 开发一个 Demo Agent 很简单，但让它在生产环境稳定运行，需要解决一系列隐藏的工程难题。这里汇总 12 个经典问题及 DeerFlow 的解法。

---

## 面试题 1：Agent 死循环怎么办？

**场景**: LLM 反复调用同一个工具，每次都失败，但不会主动停止。

**解法**: 双重循环检测

```python
# 哈希检测：精确重复（同一工具 + 同一参数）
窗口 20 轮内重复 3 次 → 追加警告到 AIMessage
窗口 20 轮内重复 5 次 → 移除 tool_calls，强制文本输出

# 频率检测：同一工具调用过多次（不要求参数相同）
bash 调用 ≥30 次 → 警告
bash 调用 ≥50 次 → 强停
```

**为什么两层**: 哈希检测精确但只能抓"完全一样"的重复；频率检测能抓"换参数但本质一样"的重复。

源码: `agents/middlewares/loop_detection_middleware.py`

---

## 面试题 2：上下文窗口满了怎么办？

**场景**: 对话越来越长，token 接近模型上限。

**解法**: 三阶段压缩

```
120 条消息（10 万 token）
    │
    ▼ 第一阶段：基础分区
系统提示 + 最近 N 条保留，中间的标为可压缩
    │
    ▼ 第二阶段：技能救援
从可压缩区捞出技能调用（Agent 还在用）
    │
    ▼ 第三阶段：Reminder 保护
从可压缩区捞出 todo_reminder（任务没完成）
    │
    ▼ 剩余可压缩区 → LLM 生成摘要 → 1 条消息替换
    │
    ▼ 结果：41 条消息（4 万 token）
```

**关键**: 三阶段递进保护，每阶段比上阶段更精细地保留重要信息。

源码: `agents/middlewares/summarization_middleware.py`

---

## 面试题 3：多个用户同时操作同一个 Thread 怎么办？

**场景**: 用户在手机上发了一条消息，同时在电脑上又发了一条。

**解法**: 三种并发策略

| 策略 | 行为 | 适用场景 |
|------|------|---------|
| `reject` | 新请求返回 409 | 长任务不允许打断 |
| `interrupt` | 取消当前 Run，保留检查点，新请求从最新状态继续 | 用户改主意了 |
| `rollback` | 取消当前 Run，回滚到 Run 开始前状态 | 需要干净重试 |

```python
class LockManager:
    def __init__(self):
        self._thread_locks = defaultdict(asyncio.Lock)
```

源码: `runtime/lock_manager.py`

---

## 面试题 4：Agent 执行到一半崩溃了怎么恢复？

**场景**: Agent 执行了 10 步工具调用后服务重启。

**解法**: LangGraph 检查点 + 事件流持久化

```
执行过程：
Step 1 → Checkpoint 1 → RunEvent 1,2,3
Step 2 → Checkpoint 2 → RunEvent 4,5
...
Step 10 → Checkpoint 10 → RunEvent 28,29,30
                         ↑ 崩溃
恢复：从 Checkpoint 10 继续
审计：通过 RunEvent 回放完整过程
```

三个独立机制互为补充：
- **Checkpointer**: LangGraph 内置，状态恢复
- **RunJournal**: LangChain 回调，事件审计
- **StreamBridge**: 实时推送，前端渲染

源码: `runtime/checkpointer/`, `runtime/journal.py`

---

## 面试题 5：如何防止 Agent 执行危险操作？

**场景**: Agent 调用 `bash("rm -rf /")` 或访问敏感文件。

**解法**: 护栏 + 沙箱双层防护

```
工具调用 → 护栏层（能不能调这个工具？）
             │
             ▼ 允许
         沙箱层（调了之后能访问什么？）
```

护栏: 白名单/黑名单工具 + 外部策略引擎
沙箱: 虚拟路径映射 + 路径穿越防护 + 只读挂载

源码: `guardrails/middleware.py`, `sandbox/security.py`

---

## 面试题 6：LLM API 调用失败了怎么办？

**场景**: OpenAI/Anthropic API 返回 429 限流或 500 服务端错误。

**解法**: 指数退避重试 + 熔断器

```python
重试策略:
第 1 次失败 → 等 1s → 重试
第 2 次失败 → 等 2s → 重试
第 3 次失败 → 等 4s → 重试
超过最大重试 → 熔断器打开

熔断器:
Closed（正常）→ 连续 5 次失败 → Open（熔断）
Open → 等 60s → Half-Open → 试一次
    ├── 成功 → Closed
    └── 失败 → Open
```

**不重试的**: `BadRequestError`（请求本身有问题，重试没用）

源码: `agents/middlewares/llm_error_handling_middleware.py`

---

## 面试题 7：如何让 Agent 记住用户的偏好？

**场景**: 用户说了三次"我喜欢 TypeScript"，Agent 每次都忘。

**解法**: 被动提取 + 防抖更新 + 置信度筛选

```
用户对话中提到偏好
    │
    ▼ LLM 被动提取（不需要用户显式声明）
    │
    ▼ 置信度打分（≥0.7 才保留）
    │
    ▼ 放入队列，30 秒防抖
    │
    ▼ 批量写入 memory.json

下次对话:
    │
    ▼ 注入到系统提示（2000 token 预算）
    │
    ▼ 按置信度降序，超出预算的截断
```

三重防膨胀: 100 条上限 + 0.7 置信度门槛 + 2000 token 注入预算

源码: `agents/memory/`, `agents/middlewares/memory_middleware.py`

---

## 面试题 8：Agent 的中间件怎么排序？

**场景**: 系统有 20 个中间件，顺序错了会导致 bug（比如 todo 提醒被压缩掉了）。

**解法**: 声明式约束 + 固定装配顺序

```python
# 装配顺序（关键约束）
DynamicContext → Summarization → Todo → ViewImage

# 为什么这个顺序：
# Summarization 在 Todo 之前 → 压缩时不影响 todo_reminder
# Todo 在 ViewImage 之前 → todo 注入不会被图片描述干扰
```

新增中间件不修改已有代码，只声明依赖关系。

源码: `agents/lead_agent/agent.py` → `_build_middlewares()`

---

## 面试题 9：子 Agent 创建子 Agent 怎么防止无限递归？

**场景**: 主 Agent 派了一个子 Agent，子 Agent 又派了一个子 Agent，无限嵌套。

**解法**: 工具剥夺 + 并发限制 + 超时兜底

```python
# 子 Agent 的工具列表不包含 task
GENERAL_PURPOSE_CONFIG = SubagentConfig(
    disallowed_tools=["task", "ask_clarification", "present_files"]
)

# 并发限制
max_concurrent: 3  # 最多 3 个子 Agent 同时运行

# 超时兜底
timeout_seconds: 900  # 15 分钟后强制终止
```

**单层委派模型**: 子 Agent 不能再创建子 Agent。

源码: `subagents/executor.py`, `subagents/registry.py`

---

## 面试题 10：如何支持多个 LLM 提供商？

**场景**: 需要同时支持 OpenAI、Claude、自部署 vLLM，每个有不同的 API 和认证方式。

**解法**: 模型工厂 + 反射加载

```python
# 配置驱动，不需要改代码
models:
  providers:
    claude:
      model: "claude-sonnet-4-20250514"
    vllm:
      model: "Qwen/Qwen3-32B"
      base_url: "http://gpu-server:8000/v1"

# 反射加载: "module.path:ClassName" → 动态导入
provider_class = resolve("deerflow.models.claude_provider:ClaudeProvider")
```

八步管线: 配置解析 → Provider 查找 → 反射加载 → Thinking 分配 → 特殊处理 → 实例化 → 追踪装饰

源码: `models/factory.py`, `reflection/resolvers.py`

---

## 面试题 11：多租户场景下怎么隔离用户数据？

**场景**: 用户 A 的 Agent 不应该能看到用户 B 的文件。

**解法**: 路径隔离 + 沙箱映射

```
物理路径: .deer-flow/users/{user_id}/threads/{thread_id}/user-data/
虚拟路径: /mnt/user-data/workspace  (Agent 视角)

用户 A 的 Agent 看到 /mnt/user-data/workspace
  → 实际映射到 .deer-flow/users/a/threads/1/user-data/workspace

用户 B 的 Agent 也看到 /mnt/user-data/workspace
  → 实际映射到 .deer-flow/users/b/threads/2/user-data/workspace

路径穿越攻击: /mnt/user-data/../../etc/passwd → 被路径校验拒绝
```

源码: `sandbox/security.py`, `agents/middlewares/thread_data_middleware.py`

---

## 面试题 12：如何在不改代码的情况下扩展 Agent？

**场景**: 需要加一个新的工具、新的认证方式、新的沙箱实现。

**解法**: 配置驱动的插件架构

| 扩展点 | 配置方式 | 例子 |
|--------|---------|------|
| 新工具 | `@tool` 装饰器 + config | 自定义搜索工具 |
| 新中间件 | 继承 `AgentMiddleware` | 自定义审计 |
| 新 Provider | 实现 `GuardrailProvider` | OPA 策略引擎 |
| 新沙箱 | 实现 `SandboxProvider` | Docker 容器 |
| 新通道 | 实现 Channel Adapter | 微信机器人 |
| 新记忆存储 | 实现 `MemoryStorage` | Redis 后端 |

```yaml
# 只改配置，不改源码
guardrails:
  provider: "my_company.custom:MyPolicyProvider"
memory:
  storage_class: "my_app.redis_memory:RedisMemoryStorage"
```

源码: `reflection/resolvers.py`, `guides/02-extension-guide.md`

---

## 数据流概览

```
生产 Agent 的完整防护体系:

请求入口
    ├── 认证 (JWT/OAuth/Internal)
    ├── CSRF 保护
    └── 并发控制 (Lock + 策略)

Agent 执行
    ├── 上下文管理 (压缩 + 记忆注入)
    ├── 安全防护 (护栏 + 沙箱)
    ├── 错误处理 (工具异常 + LLM 重试 + 熔断)
    ├── 循环检测 (哈希 + 频率)
    └── 子 Agent 控制 (递归防护 + 并发限制)

执行输出
    ├── 实时推送 (StreamBridge → SSE)
    ├── 持久化 (Checkpointer + RunJournal)
    └── 追踪 (LangSmith/Langfuse)
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 中间件装配 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` |
| 运行时管理 | `backend/packages/harness/deerflow/runtime/` |
| 全部中间件 | `backend/packages/harness/deerflow/agents/middlewares/` |
| 扩展指南 | `docs/guides/02-extension-guide.md` |

## 深入阅读

- [架构决策](../guides/01-architecture-decisions.md) — 系统级设计选择
- [Agent 设计决策](../core/agent/06-design-decisions.md) — 中间件设计
- [工具调用失败处理](022-工具调用失败处理.md) — 完整错误处理链
- [Agent 成本控制](024-Agent成本控制与Token管理.md) — Token 和成本优化
