# 记忆系统心智模型

> 来源：`backend/packages/harness/deerflow/agents/memory/`（updater、queue、storage、prompt）、`agents/middlewares/memory_middleware.py`、`config/memory_config.py`

## 1. 写时分离——中间件只负责"入队"，不负责"更新"

`MemoryMiddleware` 在 `after_agent` 钩子中做的事极其克制：过滤消息 → 排入队列 → 返回 None。它不调用 LLM、不读写文件、不阻塞 Agent 执行。真正的记忆更新发生在 `MemoryUpdateQueue` 的后台线程中——防抖计时器到期后才创建 `MemoryUpdater`，调用 LLM 分析对话，提取事实，写入存储。

不要在请求路径中执行 LLM 调用或文件 I/O 来更新记忆。Agent 的响应延迟是用户体验的核心——每多等一秒，用户就多一分焦虑。`MemoryMiddleware` 把"记住了什么"这个有成本的操作从请求路径中剥离，变成异步、可批量的后台任务。这种"写时分离"在数据库领域是 CQRS 的变体：读路径（注入记忆到 prompt）是同步的、廉价的；写路径（LLM 提取事实）是异步的、昂贵的。中间件是两者之间的唯一桥梁。

## 2. 防抖 + 线程去重——高频对话不会淹没记忆管道

`MemoryUpdateQueue` 实现了两层保护：**防抖**（debounce）和**线程去重**。每次 `add()` 调用都重置 30 秒计时器，在窗口内到达的新会话替换同 `thread_id` 的旧条目（`self._queue = [c for c in self._queue if c.thread_id != thread_id]`）。计时器到期时，队列中所有条目批量处理，条目间加 500ms 间隔避免 LLM 限流。

不要为每次对话都触发一次 LLM 调用。用户可能在 30 秒内连续发 5 条消息，每条触发一次 `after_agent`。如果逐条处理，5 次 LLM 调用的成本和延迟都不可接受。防抖让系统自然聚合一次"交互会话"——用户停下来 30 秒后才处理，此时能看到完整对话脉络，LLM 提取的事实质量也更高。线程去重确保同一个对话的多次更新不会重复排队。

## 3. 消息过滤的三重策略——信号提取而非全量灌入

进入记忆管道的消息经过三层过滤：

- **中间件层**（`_filter_messages_for_memory`）：丢弃所有 `tool` 消息和带 `tool_calls` 的 AI 消息，只保留用户输入和最终 AI 响应。如果用户消息仅包含 `<uploaded_files>` 块（纯文件上传、无文本），连同配对的 AI 响应一起跳过
- **格式化层**（`format_conversation_for_update`）：剥离 `<uploaded_files>` 标签，截断超过 1000 字符的消息，只保留 human/ai 两种角色
- **更新器层**（`_strip_upload_mentions_from_memory`）：LLM 返回更新后，用正则从所有摘要和事实中移除描述文件上传事件的句子

不要把 Agent 的完整对话历史（含工具调用、中间推理、文件路径）灌给 LLM 做记忆提取。工具调用是 Agent 的内部实现细节，对理解用户偏好毫无帮助。全量灌入浪费 token，还可能让 LLM 把工具调用模式当成"用户行为"记录下来。三层过滤确保进入 LLM 的只有**人类意图 + Agent 最终回应**——这是记忆真正需要保留的信号。上传文件的处理尤其值得注意：文件路径是会话范围的，如果被记录到长期记忆中，下次对话时 Agent 会去查找一个已经不存在的文件。

## 4. 事实系统的置信度门控 + 容量淘汰——记忆不是垃圾桶

`MemoryUpdater._apply_updates` 对事实实施了两个约束：**置信度门控**（低于 `fact_confidence_threshold` 的新事实直接丢弃，默认 0.7）和**容量淘汰**（超过 `max_facts` 时按置信度排序，保留最高的）。新事实还通过 `_fact_content_key` 做**内容去重**——对 content 做 `strip()` 后比较，已有相同内容的事实不再追加。LLM 可以通过 `factsToRemove` 字段显式请求删除过时事实。

不要把 LLM 提取的每个"事实"都无条件存入记忆。LLM 是概率模型，低置信度的输出可能是幻觉。0.5 置信度的事实（"用户似乎喜欢 X"）不值得占用有限的事实槽位。容量淘汰确保记忆系统不会无限膨胀——当事实数量达到上限时，新的事实必须比旧的更有把握才能挤入。这种"有限容量 + 优胜劣汰"的设计和人类记忆的自然衰减是同一思路：不是所有经历都值得长期记住，记住的应该是反复出现、确信度高的模式。

## 5. 原子写入 + mtime 缓存失效——多进程场景下的存储安全

`FileMemoryStorage.save` 使用临时文件 + 重命名（`temp_path.replace(file_path)`）实现原子写入。`load` 通过比较文件 `st_mtime` 判断缓存是否过期，`reload` 强制清除缓存重新读取。`get_memory_storage` 通过反射加载配置中的存储类，失败时回退到 `FileMemoryStorage`。

不要在写入文件时直接覆盖目标文件。写入过程中如果进程崩溃（OOM、SIGKILL），文件会变成半截的损坏状态，下次加载时 JSON 解析失败导致记忆丢失。先写临时文件再原子替换（`rename` 在 POSIX 上是原子操作），确保目标文件要么是旧的完整版本，要么是新的完整版本，不存在中间态。mtime 缓存让 Gateway 和 LangGraph 两个进程能各自维护本地缓存，同时通过文件修改时间检测对端的写入——这与 [[006-skills]] 中的跨进程配置一致性模式完全一致：**写入端保证落盘，读取端不信任缓存**。

## 6. Token 预算制的记忆注入——信息检索的精确计量

`format_memory_for_injection` 不是把所有记忆一股脑塞进 prompt。它按置信度排序事实，逐条计算 token 数（优先用 tiktoken，回退到 `len/4` 估算），累加到 `max_injection_tokens`（默认 2000）预算耗尽时停止。超预算时按 95% 字符比例截断并追加省略号。注入的内容用 `<memory>` XML 标签包裹，嵌入到系统提示词模板的 `{memory_context}` 占位符中。

不要把记忆的"存"和"用"等同起来。记忆文件可能有 100 条事实 + 多个摘要段落，全部注入会占据大量 prompt 空间，挤压 Agent 处理实际任务的 context window。Token 预算制让记忆注入变成一个**背包问题**：在有限预算内装入置信度最高的信息。摘要和上下文无条件包含（它们是结构化的、经过 LLM 精炼的），事实按置信度降序逐条填充剩余预算。这和搜索引擎的"Top-K 结果截断"是同一思路——用户不需要看到全部结果，只需要最相关的。
