"""DeerFlow API Gateway 核心包。

本包是 DeerFlow 后端的 HTTP 入口层，基于 FastAPI 构建的 API 网关。
网关承担以下职责：
  - 提供 RESTful API 端点供前端和外部客户端调用
  - 嵌入 LangGraph 兼容的 Agent 运行时，支持流式/阻塞/后台运行模式
  - 统一的身份认证与授权拦截（JWT + CSRF + 内部令牌）
  - IM 频道（飞书/Slack/Telegram/钉钉）消息桥接

核心模块说明：
  - app.py        — FastAPI 应用工厂，生命周期管理，路由挂载
  - config.py     — 网关配置（监听地址、端口、文档开关等）
  - auth_middleware.py  — 全局认证中间件，JWT 校验 + 用户上下文注入
  - csrf_middleware.py  — CSRF 防护中间件（Double Submit Cookie 模式）
  - authz.py      — 授权装饰器（@require_auth / @require_permission）
  - deps.py       — FastAPI 依赖注入：单例获取器、运行时初始化
  - internal_auth.py   — 进程内内部调用认证（频道 Worker → Gateway）
  - langgraph_auth.py  — LangGraph Server 兼容认证处理器
  - services.py   — 运行生命周期服务层（创建运行、SSE 格式化）
  - path_utils.py — 线程虚拟路径解析（沙箱路径 → 宿主机路径）
  - utils.py      — 通用工具函数

子包：
  - auth/         — 完整的身份认证子系统（JWT、密码哈希、用户仓库等）
"""

from .app import app, create_app
from .config import GatewayConfig, get_gateway_config

__all__ = ["app", "create_app", "GatewayConfig", "get_gateway_config"]
