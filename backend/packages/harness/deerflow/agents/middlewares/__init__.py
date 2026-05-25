"""中间件子包。

本包包含约 20 个中间件实现，构成 DeerFlow Agent 的横切关注点处理链。
中间件按固定顺序串联，每个中间件在特定的生命周期钩子中执行：

  before_agent → wrap_model_call → after_model → wrap_tool_call → after_agent

中间件执行顺序（Lead Agent 完整链）：
  [0]  ThreadDataMiddleware       → 创建线程目录（workspace/uploads/outputs）
  [1]  UploadsMiddleware          → 注入上传文件信息到 HumanMessage
  [2]  SandboxMiddleware         → 配置沙箱执行环境
  [3]  DanglingToolCallMiddleware → 修补悬挂的工具调用（用户中断导致）
  [4]  GuardrailMiddleware       → 安全护栏（可选）
  [5]  ToolErrorHandlingMiddleware → 工具异常转 ToolMessage
  [6]  LLMErrorHandlingMiddleware → LLM 错误重试 + 熔断器
  [7]  SandboxAuditMiddleware    → Bash 命令安全审计
  [8]  SummarizationMiddleware   → 对话摘要压缩（token 接近上限时触发）
  [9]  DynamicContextMiddleware  → 记忆/日期动态注入
  [10] TodoMiddleware            → 任务追踪 + 防提前退出
  [11] TokenUsageMiddleware      → Token 用量统计 + 步骤归属
  [12] TitleMiddleware           → 自动标题生成
  [13] MemoryMiddleware          → 记忆更新排队（防抖 30s）
  [14] ViewImageMiddleware       → 图像内容注入（仅视觉模型）
  [15] DeferredToolFilterMiddleware → 延迟工具过滤（tool_search）
  [16] SubagentLimitMiddleware  → 子代理并发限制
  [17] LoopDetectionMiddleware  → 循环检测 + 强制停止
  [18] ClarificationMiddleware  → 澄清请求拦截（始终最后）
"""
