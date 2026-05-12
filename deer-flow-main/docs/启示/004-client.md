# 嵌入式客户端启示

> 来源：`backend/packages/harness/deerflow/client.py`

## 1. 进程内调用复用核心模块，靠契约测试防止接口漂移

`DeerFlowClient` 直接导入 `deerflow` 内部模块（`_build_middlewares`、`apply_prompt_template`、`create_chat_model`、`get_available_tools`），在进程内构建 Agent 并执行。所有 Gateway API 的返回格式在客户端方法中手动构造为字典，保证结构与 Gateway Pydantic 模型一致。CI 中 `TestGatewayConformance` 将客户端输出喂给 Gateway 模型做校验，任何字段漂移立即被捕获。

不要让客户端调 Gateway HTTP 来复用逻辑——那样强依赖两个服务进程在线，且引入网络开销和序列化成本。也不要让客户端和 Gateway 共享响应类（会拉入 FastAPI 依赖）。进程内调用零依赖、零网络，接口一致性靠契约测试保证而非共享代码。一套核心逻辑、两种接入方式（HTTP / 嵌入），消费者代码无需修改即可切换。

## 2. 延迟创建 + 配置键缓存——按需构建，变更时刷新

Agent 不在 `__init__` 中创建，而是延迟到首次 `stream()` / `chat()` 调用时由 `_ensure_agent()` 构建。构建后将 `(model_name, thinking_enabled, is_plan_mode, subagent_enabled)` 四元组缓存为 `_agent_config_key`。后续调用若 key 相同则复用；不同则重建。`reset_agent()` 和配置变更方法（`update_mcp_config`、`update_skill`）将 `_agent` 置 None，强制下次调用时重建。`thread_id` 不参与 key——thread 隔离由 checkpointer 处理，不影响 Agent 实例。

不要每次调用都重建 Agent（构建成本高：模型初始化、工具加载、中间件组装）。也不要把所有参数都塞进缓存 key——只包含"影响 Agent 构造结果"的参数，不多不少。缓存 key 精确匹配构造依赖，变更触发重建，不变时复用。这是构造成本高但实例可复用对象的通用模式。

## 3. 嵌入模式输出与 LangGraph SSE 协议同构——一层 StreamEvent 隔离底层变化

`stream()` 返回 `Generator[StreamEvent]`，事件类型（`values`、`messages-tuple`、`end`）和 data 结构与 LangGraph SSE 流式协议完全一致。`chat()` 是 `stream()` 的便捷封装，只取最后一条 AI 文本。消息去重通过 `seen_ids: set[str]`，token 用量通过 `cumulative_usage` 累计。客户端用 `_serialize_message` 和 `_extract_text` 做一层薄序列化，逻辑与 Gateway 对齐。

不要让嵌入模式直接返回 LangGraph 内部 chunk——那样消费者需要理解 LangGraph 状态结构，且 HTTP 模式和嵌入模式看到的数据格式不同，迁移成本翻倍。用 `StreamEvent` 作为稳定抽象层，消费者在 HTTP 流式和嵌入模式之间无缝切换，事件处理逻辑完全复用。先以嵌入模式快速开发调试（无服务依赖），生产环境切 HTTP 模式（可水平扩展），代码零改动。底层 LangGraph 版本变化的影响被 `StreamEvent` 层隔离。
