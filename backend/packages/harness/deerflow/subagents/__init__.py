"""子代理（Subagent）任务委派系统。

本模块实现了 Lead Agent（主代理）的任务委派能力。当主代理面对复杂、多步骤或
需要特定专业知识的任务时，可以通过 task() 工具将子任务委派给专用子代理执行。

核心架构:
    - 双线程池模型: _scheduler_pool（3 工作线程）负责调度编排，
      _execution_pool 通过持久化事件循环（isolated event loop）执行异步代理运行
    - 并发控制: MAX_CONCURRENT_SUBAGENTS = 3，由 SubagentLimitMiddleware 在
      after_model 阶段截断多余的 task 工具调用
    - 事件驱动: task_started → task_running → task_completed / task_failed / task_timed_out
    - 默认超时: 15 分钟（900 秒）

模块结构:
    - config.py: SubagentConfig 数据类定义，模型名称解析
    - executor.py: SubagentExecutor 执行引擎，双线程池，超时处理，SSE 事件发射
    - registry.py: 代理注册与发现，内置 + 自定义代理合并，config.yaml 覆盖
    - token_collector.py: 子代理 LLM 调用的 token 用量收集
    - builtins/: 内置代理配置（general-purpose, bash）

导出:
    SubagentConfig: 子代理配置数据类
    SubagentExecutor: 子代理执行器（同步/异步执行、后台任务）
    SubagentResult: 执行结果数据类（状态、结果消息、token 用量）
    get_available_subagent_names: 获取当前运行时可用的子代理名称列表
    get_subagent_config: 按名称查找子代理配置（含 config.yaml 覆盖）
    list_subagents: 列出所有已注册的子代理配置
"""

from .config import SubagentConfig
from .executor import SubagentExecutor, SubagentResult
from .registry import get_available_subagent_names, get_subagent_config, list_subagents

__all__ = [
    "SubagentConfig",
    "SubagentExecutor",
    "SubagentResult",
    "get_available_subagent_names",
    "get_subagent_config",
    "list_subagents",
]
