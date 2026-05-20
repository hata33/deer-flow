# 04 - 并发控制：多任务策略

## 问题背景

用户在同一线程上快速连续发送消息时，可能出现多个运行同时请求执行的情况。但同一线程上的运行共享 LangGraph 的检查点状态——如果两个运行同时读写同一个检查点，会导致状态混乱。

DeerFlow 提供三种多任务策略来解决这个问题，由前端在创建运行时通过 `multitask_strategy` 参数指定。

## 策略一：reject（拒绝）

### 做了什么

当线程上已有进行中的运行时，直接拒绝新请求，返回 HTTP 409 Conflict。

### 执行步骤

1. 前端发送新消息
2. Gateway 调用 `RunManager.create_or_reject(multitask_strategy="reject")`
3. RunManager 在 Lock 保护下检查：该线程是否有 pending/running 状态的运行？
4. 如果有 → 抛出 ConflictError，Gateway 返回 409
5. 如果没有 → 创建新运行

### 适合场景

对状态一致性要求严格的场景。用户必须等当前运行完成才能发下一条消息。这是默认策略。

### 设计考量

这是最简单、最安全的策略。没有副作用，不需要取消任何运行，不需要回滚状态。但它要求用户等待，可能导致体验不够流畅。

## 策略二：interrupt（中断）

### 做了什么

取消当前正在进行的运行，然后创建新运行。被取消的运行保留其检查点状态。

### 执行步骤

1. 前端发送新消息，指定 `multitask_strategy="interrupt"`
2. Gateway 调用 `RunManager.create_or_reject(multitask_strategy="interrupt")`
3. RunManager 在 Lock 保护下：
   - 找到线程上所有进行中的运行
   - 设置它们的 abort_event（通知它们停止）
   - 设置它们的 abort_action 为 "interrupt"
   - 取消它们的 asyncio Task
   - 将状态标记为 interrupted
   - 持久化状态变更到数据库
4. 创建新运行
5. 被取消的运行在 run_agent 中：
   - 检测到 abort_event 被设置，停止迭代
   - 因为 abort_action 是 "interrupt"，状态保持 interrupted
   - 检查点**不被回滚**——保留了中断时的状态

### 适合场景

用户想要"改主意"的场景。比如 Agent 正在执行一个长时间任务，用户发送了新指令。使用 interrupt 可以让 Agent 立即停下来处理新请求，但之前的工作不会被撤销。

### 设计考量

interrupt 的关键设计是"检查点保留"。LangGraph 的检查点记录了对话的完整状态，如果中断后不保留检查点，用户就无法从断点继续对话。保留检查点意味着 Agent 的部分工作成果被保留，下次可以从断点恢复。

## 策略三：rollback（回滚）

### 做了什么

取消当前运行，并将线程状态恢复到该运行开始之前的检查点。就像被取消的运行从未发生过一样。

### 执行步骤

1. 前端发送新消息，指定 `multitask_strategy="rollback"`
2. Gateway 调用 `RunManager.create_or_reject(multitask_strategy="rollback")`
3. RunManager 在 Lock 保护下取消进行中的运行（同 interrupt）
4. 创建新运行
5. 被取消的运行在 run_agent 中：
   - 检测到 abort_event，停止迭代
   - 因为 abort_action 是 "rollback"：
     a. 调用 `_rollback_to_pre_run_checkpoint()`
     b. 创建一个新的检查点标记（新 ID + 新时间戳）
     c. 将保存的运行前检查点数据写入 Checkpointer
     d. 恢复运行前的 pending_writes

### 适合场景

需要"完全干净地重新开始"的场景。比如 Agent 在执行过程中产生了错误的中间状态，用户希望从头开始而不是从中断点继续。

### 设计考量

rollback 的实现有几个关键技术点：

**为什么需要提前保存快照**：检查点是 LangGraph 管理的，run_agent 无法控制检查点的写入时机。Agent 执行的每个节点都可能修改检查点。如果不提前保存运行前的快照，取消时就无法知道要恢复到哪个状态。

**为什么恢复时创建新 ID 而不是复用旧 ID**：Checkpointer 可能对检查点 ID 有唯一性约束或版本管理。创建新 ID 避免了与已有检查点的冲突，同时新时间戳正确反映了恢复操作的时间。

**为什么恢复 pending_writes**：检查点的 pending_writes 是 LangGraph 内部的写入队列——节点执行完成但尚未应用到检查点的写入。如果不恢复这些写入，可能会丢失运行前已提交但未应用的数据。

## Lock 保护的重要性

所有策略的检查和操作都在 `asyncio.Lock` 保护下执行。这消除了 TOCTOU（Time-of-check to time-of-use）竞争条件——如果没有 Lock，两个请求可能同时检查"线程是否有进行中的运行"，都看到没有，然后都创建新运行，导致同一个线程上有两个并发运行。

Lock 确保了"检查 + 操作"是原子的：在检查和创建之间，不会有其他请求插入。

## cancel 的幂等性

`RunManager.cancel()` 方法被设计为幂等的——对一个已经中断的运行再次调用 cancel 会返回 True 而不是报错。这是因为在异步环境中，取消请求可能在运行已经自然结束后才到达。幂等性确保取消操作始终是安全的，不会因为时机问题导致异常。
