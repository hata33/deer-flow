"""DeerFlow 智能体系统入口模块。

本模块是 agents 包的唯一入口，负责：
1. 导出核心公共 API（create_deerflow_agent、RuntimeFeatures、make_lead_agent 等）
2. 在模块导入时预热技能缓存（prime_enabled_skills_cache），
   使后续请求路径能直接读取热缓存，避免同步文件 I/O 阻塞 prompt 模块导入

模块架构总览：
  agents/
  ├── __init__.py          ← 本文件，包入口 + 技能缓存预热
  ├── factory.py           → 纯参数工厂 create_deerflow_agent()（SDK 级入口）
  ├── features.py          → RuntimeFeatures 特性标志 + @Next/@Prev 中间件定位装饰器
  ├── thread_state.py      → ThreadState 状态模式（LangGraph 状态定义）
  ├── lead_agent/          → 应用层工厂 make_lead_agent()（配置驱动）
  │   ├── agent.py         → 中间件链组装 + 模型解析 + 图构建
  │   └── prompt.py        → 系统提示词模板 + 技能缓存管理
  ├── memory/              → 跨会话记忆系统（四层架构）
  │   ├── prompt.py        → 第 1+3 层：注入格式化 + 更新提示词
  │   ├── storage.py       → 第 2 层：JSON 文件持久化
  │   ├── updater.py       → 第 3 层：LLM 驱动的记忆提取
  │   ├── queue.py         → 第 4 层：防抖队列
  │   ├── message_processing.py → 消息过滤 + 信号检测
  │   └── summarization_hook.py → 摘要前记忆刷入钩子
  └── middlewares/         → 中间件链（约 20 个中间件）
      ├── ThreadDataMiddleware   → 线程目录管理
      ├── UploadsMiddleware      → 上传文件注入
      ├── SandboxMiddleware      → 沙箱执行环境
      ├── DanglingToolCallMiddleware → 修补悬挂工具调用
      ├── LLMErrorHandlingMiddleware → LLM 错误重试 + 熔断
      ├── SandboxAuditMiddleware → Bash 命令安全审计
      ├── ToolErrorHandlingMiddleware → 工具异常转 ToolMessage
      ├── GuardrailMiddleware   → 安全护栏
      ├── SummarizationMiddleware → 对话摘要压缩
      ├── DynamicContextMiddleware → 记忆/日期动态注入
      ├── TodoMiddleware        → 任务追踪 + 防提前退出
      ├── TokenUsageMiddleware  → Token 用量统计 + 步骤归属
      ├── TitleMiddleware       → 自动标题生成
      ├── MemoryMiddleware      → 记忆更新排队
      ├── ViewImageMiddleware   → 图像内容注入
      ├── DeferredToolFilterMiddleware → 延迟工具过滤
      ├── SubagentLimitMiddleware → 子代理并发限制
      ├── LoopDetectionMiddleware → 循环检测 + 强制停止
      └── ClarificationMiddleware → 澄清请求拦截

导出列表：
  - create_deerflow_agent：SDK 级纯参数工厂
  - RuntimeFeatures：声明式特性标志
  - Next / Prev：中间件定位装饰器
  - make_lead_agent：应用层配置驱动工厂
  - SandboxState / ThreadState：状态模式定义
"""

from .factory import create_deerflow_agent
from .features import Next, Prev, RuntimeFeatures
from .lead_agent import make_lead_agent
from .lead_agent.prompt import prime_enabled_skills_cache
from .thread_state import SandboxState, ThreadState

# LangGraph imports deerflow.agents when registering the graph. Prime the
# enabled-skills cache here so the request path can usually read a warm cache
# without forcing synchronous filesystem work during prompt module import.
prime_enabled_skills_cache()

__all__ = [
    "create_deerflow_agent",
    "RuntimeFeatures",
    "Next",
    "Prev",
    "make_lead_agent",
    "SandboxState",
    "ThreadState",
]
