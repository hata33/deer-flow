# 特性系统与策略

本文档描述 DeerFlow Agent 的声明式特性系统、中间件定位策略、错误处理策略、安全策略和其他运行时策略。

## 声明式特性系统

### RuntimeFeatures 数据类

每个特性字段接受三种值：

| 值 | 行为 |
|-----|------|
| `True` | 使用内置默认中间件 |
| `False` | 禁用该特性 |
| `AgentMiddleware 实例` | 使用自定义实现替换内置默认 |

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True          # 沙箱基础设施
    memory: bool | AgentMiddleware = False          # 记忆系统
    summarization: Literal[False] | AgentMiddleware = False  # 摘要压缩（需 model 参数）
    subagent: bool | AgentMiddleware = False        # 子代理
    vision: bool | AgentMiddleware = False          # 视觉理解
    auto_title: bool | AgentMiddleware = False      # 自动标题
    guardrail: Literal[False] | AgentMiddleware = False  # 安全护栏（需 provider）
    loop_detection: bool | AgentMiddleware = True   # 循环检测
```

**特殊字段**：`summarization` 和 `guardrail` 没有内置默认中间件（因为它们需要额外的构造参数），只接受 `False` 或自定义实例。

### @Next / @Prev 定位装饰器

用于 `create_deerflow_agent()` 的 `extra_middleware` 参数中，声明中间件在链中的相对位置：

- `@Next(A)` → 放在 A 类中间件**之后**
- `@Prev(A)` → 放在 A 类中间件**之前**
- 不能同时使用 `@Next` 和 `@Prev`

```python
@Next(ClarificationMiddleware)
class MyMiddleware(AgentMiddleware):
    ...

agent = create_deerflow_agent(
    model=model,
    tools=tools,
    features=RuntimeFeatures(loop_detection=True),
    extra_middleware=[MyMiddleware()],
)
```

### 额外中间件插入算法

`_insert_extra()` 的插入策略：

1. **验证**：不允许同时有 `@Next` 和 `@Prev`
2. **冲突检测**：两个 extra 瞄准同一锚点时报错
3. **未锚定中间件**：插入到 `ClarificationMiddleware` 之前
4. **锚定中间件**：迭代插入（支持 extra 之间互相锚定）
5. **不变量**：`ClarificationMiddleware` 始终在链尾

## 错误处理策略

### LLM 错误重试 + 熔断器

`LLMErrorHandlingMiddleware` 实现了完整的错误恢复机制：

```
错误分类：
  transient（瞬态）  → 超时、连接断开、5xx → 可重试
  busy（繁忙）       → 429、rate limit → 可重试
  quota（配额不足）  → billing/credit → 不可重试
  auth（认证失败）   → API key 无效 → 不可重试
  generic（其他）    → 不可重试

重试策略：
  最大 3 次
  指数退避：1s → 2s → 4s（上限 8s）
  支持 Retry-After 头部解析
  中英文繁忙模式匹配

熔断器状态机：
  Closed → Open → Half-Open → Closed
  连续失败达阈值 → 熔断（fast fail）
  恢复超时后 → 半开（允许一次探测）
  探测成功 → 重置为 Closed
```

### 工具异常处理

`ToolErrorHandlingMiddleware` 将工具异常转换为错误 `ToolMessage`，保证对话流不中断：
- 异常信息截断到 500 字符
- 保留 `GraphBubbleUp` 信号（LangGraph 控制流）

## 安全策略

### Bash 命令审计

`SandboxAuditMiddleware` 对所有 `bash` 工具调用进行安全审计：

```
输入验证：
  空命令 → 拒绝
  超过 10,000 字符 → 拒绝
  包含 null 字节 → 拒绝

风险评估（两级）：
  高风险（block）  → rm -rf /, curl|bash, dd if=, mkfs, fork bomb 等
                    → 阻止执行，返回错误 ToolMessage
  中风险（warn）   → pip install, chmod 777, sudo/su, PATH= 等
                    → 允许执行，追加警告到结果

命令拆分：
  支持复合命令分析（; && ||）
  引号感知拆分（单引号/双引号）
  未闭合引号 → fail-closed（整条命令视为可疑）
```

### 循环检测

`LoopDetectionMiddleware` 实现双层检测：

```
第一层 — 哈希检测（identical call sets）：
  滑动窗口（默认 20）
  相同哈希 ≥ 3 次 → 注入警告
  相同哈希 ≥ 5 次 → 强制停止（剥离 tool_calls）

第二层 — 频率检测（per-tool-type）：
  追踪同一工具类型调用次数（不限参数）
  ≥ 30 次 → 注入警告
  ≥ 50 次 → 强制停止
  支持每工具频率覆盖（如 bash 允许更高频率）

工具调用键规范化：
  read_file → 路径 + 行号分桶（200 行一桶）
  write_file / str_replace → 哈希完整参数（内容敏感）
  其他工具 → 只取显著字段（path/url/query/command）
```

### 子代理并发控制

`SubagentLimitMiddleware` 硬性截断超出限制的并行 `task` 调用：
- 默认最大并发：3
- 有效范围：[2, 4]
- 超出的调用被静默丢弃（保留前 N 个）

### Guardrail 安全护栏

`GuardrailMiddleware` 提供工具调用前置授权：
- 基于 `GuardrailProvider` 协议的可插拔实现
- `AllowlistProvider`（内置，零依赖）
- OAP 策略提供者（如 `aport-agent-guardrails`）
- 自定义 Provider
- `fail_closed` 模式：无匹配规则时默认拒绝

## 提示词策略

### 静态提示词 + 前缀缓存

系统提示词被设计为**完全静态**（跨用户、跨会话相同），以最大化 LLM 前缀缓存命中率：

- 系统提示词：静态（技能列表 + 模板参数）
- 记忆/日期：通过 `DynamicContextMiddleware` 作为 `<system-reminder>` 注入到第一条 `HumanMessage`
- ID 交换技术：reminder 消息取原消息 ID（替换位置），用户内容以派生 ID 追加

### 跨日检测

如果对话跨越午夜，注入轻量日期更新提醒作为当前 `HumanMessage` 前的独立 `HumanMessage`，持久化后后续轮次看到一致的日期历史。

### 技能渐进式加载

技能不一次性加载全部内容到提示词：
1. 系统提示词仅列出技能名称、描述和文件路径
2. Agent 根据用户请求匹配技能后，调用 `read_file` 读取技能文件
3. 技能文件中的外部资源按需加载

### 澄清优先策略

系统提示词中定义了严格的 `CLARIFY → PLAN → ACT` 工作流优先级：
- 5 种必须澄清的场景：缺失信息、需求模糊、方案选择、风险确认、建议
- `ClarificationMiddleware` 拦截 `ask_clarification` 工具调用，中断执行等待用户回复

## 摘要策略

`DeerFlowSummarizationMiddleware` 扩展了 LangChain 内置的摘要中间件：

```
触发条件：token 接近上限
  → 触发类型：tokens / messages / fraction of max input

分区策略：
  1. 标准分区：cutoff 之前 → 摘要，cutoff 之后 → 保留
  2. 技能 bundle 保护：保留最近 N 个技能文件读取的 ToolMessage（不超过 token 预算）
  3. 动态上下文保护：保留 DynamicContextMiddleware 注入的 reminder 消息

Hook 机制：
  before_summarization hooks 在摘要删除消息前触发
  → memory_flush_hook：在摘要前先刷入排队中的记忆更新
```

## 记忆策略

```
消息过滤 → 仅保留 user 消息 + final AI 响应
信号检测 → 纠正信号（correction）和强化信号（reinforcement）
防抖排队 → 30s 去重，per-thread 去重
事实提取 → LLM 驱动，whitespace-normalized 去重
原子写入 → temp 文件 + rename
注入策略 → Top 15 事实 + 上下文摘要，max_injection_tokens 限制
```
