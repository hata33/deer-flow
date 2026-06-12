# 07 - 子代理状态合约：前后端结构化通信机制

> 本文档分析 `status_contract` 模块（PR #3154, issue #3146），解答"子代理终态如何可靠地从前端卡片消失"——从历史痛点到结构化合约的设计决策和实现细节。

---

## 一、问题背景

### 之前的方式：前端字符串前缀匹配

前端通过 `startsWith()` 匹配 `task` 工具返回的文本内容来判断子任务卡片状态：

```
"Task Succeeded. Result: xxx"   → startswith("Task Succeeded")  → completed
"Task failed. Error: yyy"       → startswith("Task failed")     → failed
```

### 为什么脆弱

1. **后端措辞改动静默破坏前端**：`#3107 BUG-007` 和 `#3131 review` 反复出现——后端改了一句话，前端卡片就不再关闭
2. **新增返回路径容易漏**：`task_tool.py` 有 5 个正常返回 + 3 个预执行 `Error:` 路径，每加一个都要前后端同步改
3. **异常包装绕过前缀**：`ToolErrorHandlingMiddleware` 把工具异常包装成 `Error: Tool 'task' failed with ...`，这个格式不在前端的匹配表里

### 根因

**前后端合约是隐式的（文本格式约定），不是显式的（结构化字段）。** 任何一方独立修改都会破坏合约。

---

## 二、解决方案：结构化状态字段

### 核心思路

在 `ToolMessage.additional_kwargs` 中携带结构化字段，前端优先读字段，文本匹配降级为兜底：

```
之前: ToolMessage.content = "Task Succeeded. Result: xxx"
      前端: content.startsWith("Task Succeeded") → completed

现在: ToolMessage.additional_kwargs = { subagent_status: "completed" }
      前端: additional_kwargs.subagent_status → completed（优先）
            content.startsWith(...) → completed（兜底）
```

### 数据流全链路

```
┌─────────────────────────────────────────────────────────────┐
│ task_tool.py 返回结果文本                                     │
│   "Task Succeeded. Result: ..." / "Task failed. ..." / ...   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ ToolErrorHandlingMiddleware._stamp_task_subagent_status()    │
│                                                              │
│   1. 只处理 tool_name == "task"（其他工具不碰）                │
│   2. extract_subagent_status(content) → 前缀匹配 → 状态枚举   │
│      匹配不到 → None → 不打标（流式中间 chunk）               │
│   3. make_subagent_additional_kwargs(status, error)           │
│   4. 写入 ToolMessage.additional_kwargs                      │
│                                                              │
│   ┌─────────────────────────────────────────┐                │
│   │ 成功路径: wrap_tool_call → _maybe_stamp  │                │
│   │ 异常路径: _build_error_message → stamp   │                │
│   └─────────────────────────────────────────┘                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ SSE 事件流 → 前端                                            │
│                                                              │
│   ToolMessage {                                              │
│     content: "Task Succeeded. Result: ...",                  │
│     additional_kwargs: {                                     │
│       subagent_status: "completed",                          │
│       // subagent_error: "..." (仅 failed 等异常状态)         │
│     }                                                        │
│   }                                                          │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ 前端 parseSubtaskResult(text, additionalKwargs?)              │
│                                                              │
│   if additionalKwargs?.subagent_status 存在:                  │
│     → 用结构化字段（可信）                                     │
│   else:                                                      │
│     → 回退到旧的前缀匹配（兼容历史消息）                        │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、5 个终态 + 前端 3 态折叠

### 后端 5 个状态值

| 状态 | 含义 | 来源 |
|------|------|------|
| `completed` | 任务成功完成 | task_tool 成功返回 |
| `failed` | 任务执行失败 | task_tool 失败 / 预执行错误 / middleware 异常包装 |
| `cancelled` | 用户主动取消 | task_tool 取消路径 |
| `timed_out` | 执行超时 | task_tool 超时路径 |
| `polling_timed_out` | 轮询结果超时（后台任务可能还在跑） | task_tool 轮询安全网 |

### 前端卡片 3 态

后端的 5 态在前端折叠为 3 态：

```
后端                              前端卡片
────                              ────────
completed       ────────────→    completed（绿色 ✓）
failed          ────────────→    failed（红色 ✗）
cancelled       ──┐
timed_out       ──┤─────────→    failed（保留原始 status 到 error 文本）
polling_timed_out ─┘
```

折叠设计的原因：前端卡片只有 `in_progress | completed | failed` 三个视觉状态，不需要 5 个。但原始状态保留在 error 文本中供调试。

---

## 四、前缀匹配表（`_PREFIX_TO_STATUS`）

```python
_PREFIX_TO_STATUS: tuple[tuple[str, SubagentStatusValue], ...] = (
    ("Task Succeeded. Result:", "completed"),          # ① task_tool 正常返回
    ("Task polling timed out", "polling_timed_out"),   # ② task_tool 轮询超时
    ("Task timed out", "timed_out"),                    # ③ task_tool 执行超时
    ("Task cancelled by user", "cancelled"),            # ④ task_tool 用户取消
    ("Task failed.", "failed"),                         # ⑤ task_tool 执行失败
    ("Error", "failed"),                                # ⑥ 预执行错误 + middleware 包装
)
```

**顺序至关重要**——从最具体到最宽泛：

- ② 必须在 ③ 之前：`"Task polling timed out"` 的前缀包含 `"Task timed out"`，反了会误匹配
- ⑤ 必须在 ⑥ 之前：`"Task failed."` 更具体，`"Error"` 是万能兜底
- ⑥ `("Error", "failed")` 捕获所有 `Error:` 开头的预执行错误和 middleware 异常包装

### 匹配逻辑

```python
def extract_subagent_status(content: str) -> SubagentStatusValue | None:
    trimmed = content.strip()
    for prefix, status in _PREFIX_TO_STATUS:
        if trimmed.startswith(prefix):
            return status
    return None  # 流式中间 chunk 或无法识别的内容
```

`return None` 是设计决策：非终态内容（如 `"Investigating ..."`）不打标，前端保持 `in_progress`，等真正的终态帧到达。

---

## 五、集中打标：为什么不在 task_tool 里各打各的

### 方案对比

| 方案 | 优势 | 劣势 |
|------|------|------|
| 在 task_tool 的 8 个返回分支各打标 | 直觉 | 新增返回路径容易忘记打标 |
| **在 middleware 集中打标（当前）** | **漏不掉** | 需要前缀表映射，但映射有测试覆盖 |

### 打标入口

`ToolErrorHandlingMiddleware` 有两条打标路径：

```
wrap_tool_call / awrap_tool_call
    │
    ├── 成功路径 → _maybe_stamp(result, request)
    │       → result 是 ToolMessage → _stamp_task_subagent_status()
    │       → result 是 Command → 跳过（LangGraph 控制流，不是工具输出）
    │
    └── 异常路径 → _build_error_message(request, exc)
            → 构造错误 ToolMessage
            → _stamp_task_subagent_status(error=ExcClass + detail)
```

非 `task` 工具完全不受影响——`tool_name != "task"` 时直接返回原 message。

---

## 六、跨语言合约测试（Contract Fixture）

### 单一事实来源

`contracts/subagent_status_contract.json` 定义所有测试用例：

```json
{
  "valid_status_values": ["completed", "failed", "cancelled", "timed_out", "polling_timed_out"],
  "cases": [
    {
      "name": "succeeded",
      "origin": "task_tool.py succeeded path",
      "content": "Task Succeeded. Result: investigated and produced a 3-page report",
      "expected_status": "completed",
      "expected_error_contains": null
    },
    // ... 12 个用例覆盖所有路径
  ]
}
```

### 双向测试

```
                    subagent_status_contract.json
                           │
                ┌──────────┴──────────┐
                ▼                      ▼
    backend 测试                  frontend 测试
    test_subagent_status_        subtask-result.test.ts
    contract.py                  └─ 加载 fixture → assert 映射一致
    └─ 加载 fixture → assert
       extract_subagent_status
       结果与 fixture 一致
```

任何一方的措辞漂移会在对应语言的测试中失败。后端 19 个测试 + 前端 fixture 驱动测试。

---

## 七、向后兼容策略

### 三阶段迁移

| 阶段 | 行为 | 触发条件 |
|------|------|---------|
| 当前（阶段 1） | 优先结构化字段，兜底前缀匹配 | 新消息有结构化字段；历史消息只有文本 |
| 阶段 2（遥测验证） | 收集前端命中前缀兜底的频率 | 等前端遥测显示不再命中 |
| 阶段 3（清理） | 删除前端前缀匹配分支 | 确认所有在飞消息都有结构化字段 |

### 为什么不直接删前缀匹配

1. 历史线程中的消息只有文本，没有 `additional_kwargs`
2. 未来后端新增状态枚举值时，旧版前端会 `degrade to legacy fallback` 而不是 crash
3. 前端 `parseSubtaskResult` 对未知状态值返回 `in_progress`，安全降级

---

## 八、关键源码文件索引

| 文件 | 职责 |
|------|------|
| `subagents/status_contract.py` | 状态枚举定义 + 前缀提取 + kwargs 构造 |
| `agents/middlewares/tool_error_handling_middleware.py` | 集中打标入口 |
| `contracts/subagent_status_contract.json` | 前后端共享测试 fixture |
| `tests/test_subagent_status_contract.py` | 后端合约测试（19 用例） |
| `tests/test_tool_error_handling_subagent_stamp.py` | middleware 集成测试 |
| `frontend/src/core/tasks/subtask-result.ts` | 前端结构化读取 + 前缀兜底 |
| `frontend/tests/unit/core/tasks/subtask-result.test.ts` | 前端合约测试 |

---

## 九、设计模式总结

| 模式 | 体现 |
|------|------|
| **Centralised Stamping** | 单一中间件打标，而非 N 个返回分支各打各的 |
| **Contract Testing** | 共享 fixture 确保跨语言一致性 |
| **Fail-Safe Degradation** | 未知状态 → `in_progress`，不会误关卡片 |
| **Ordered Prefix Match** | 从最具体到最宽泛，避免误匹配 |
| **Wire Format Stability** | `make_subagent_additional_kwargs` 拒绝无效状态值（`ValueError`） |
| **Non-Terminal Pass-Through** | 流式中间 chunk 不打标，前端保持等待状态 |
