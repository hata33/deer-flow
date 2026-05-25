"""Gateway 路由模块初始化文件。

本模块负责将所有 API 路由子模块汇聚到一个统一的包中，供 FastAPI 应用
在启动时统一挂载。每个子模块对应一组 RESTful 端点，覆盖不同的业务领域：

- artifacts: AI 生成产物的文件服务（含 XSS 安全防护）
- assistants_compat: LangGraph assistants 协议兼容层
- mcp: Model Context Protocol 服务器配置管理
- models: 可用 AI 模型列表查询
- skills: 技能（Skill）的 CRUD 与安装管理
- suggestions: 对话后续建议生成
- thread_runs: 线程级别的运行管理（创建/流式/取消等）
- threads: 会话线程的生命周期管理
- uploads: 文件上传、列表与删除

注意：agents、auth、channels、feedback、memory、runs 等路由由
各自模块的 __init__.py 或 gateway 主入口直接注册，不在此处导入。
"""

from . import artifacts, assistants_compat, mcp, models, skills, suggestions, thread_runs, threads, uploads

__all__ = ["artifacts", "assistants_compat", "mcp", "models", "skills", "suggestions", "threads", "thread_runs", "uploads"]
