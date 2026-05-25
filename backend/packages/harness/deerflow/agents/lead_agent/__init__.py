"""Lead Agent 子包入口。

导出 make_lead_agent() — LangGraph 图工厂，
由 LangGraph Server 在注册图时调用。

make_lead_agent vs create_deerflow_agent：
  - make_lead_agent：配置驱动，读取 config.yaml，自动解析模型、工具、中间件
  - create_deerflow_agent：纯参数，无配置文件依赖，SDK 级可编程组装
"""

from .agent import make_lead_agent

__all__ = ["make_lead_agent"]
