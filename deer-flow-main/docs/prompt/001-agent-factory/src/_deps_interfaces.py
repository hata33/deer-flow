"""工厂模块的外部依赖接口定义。

这些接口由其他模块实现。工厂模块只调用，不实现。
完整实现见各自的模块 prompt。

---
## 依赖模块清单

- 002-config-system → get_app_config(), get_model_config(), load_agent_config()
- 003-model-factory → create_chat_model()
- 004-tool-system → get_available_tools()
- 005-state-schema → ThreadState (在同目录的 thread_state.py)
- 006-prompt-template → apply_prompt_template()
- 007-middleware-system → 各中间件构造函数
"""

from typing import TypedDict, Any
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


# ============================================================
# 配置系统 (002-config-system)
# ============================================================

class ModelConfig:
    """单个模型的配置。"""
    name: str
    use: str                    # 类路径，如 "langchain_openai:ChatOpenAI"
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    # ... 其他提供商参数

class AgentConfig:
    """单个 Agent 的配置。"""
    model: str | None           # 该 Agent 使用的模型名
    tool_groups: list[str]       # 该 Agent 启用的工具分组

class AppConfig:
    """应用级配置。"""
    models: list[ModelConfig]
    token_usage: Any             # token_usage.enabled
    tool_search: Any             # tool_search.enabled

    def get_model_config(self, name: str) -> ModelConfig | None:
        """按名称查找模型配置。"""
        ...

def get_app_config() -> AppConfig:
    """返回应用级配置单例。"""
    ...

def get_model_config(name: str) -> ModelConfig | None:
    """按名称查找模型配置。等价于 get_app_config().get_model_config(name)。"""
    ...

def load_agent_config(agent_name: str | None) -> AgentConfig | None:
    """加载指定 Agent 的配置。agent_name 为 None 时返回 None。"""
    ...


# ============================================================
# 模型工厂 (003-model-factory)
# ============================================================

def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    **kwargs,
) -> BaseChatModel:
    """从配置创建聊天模型实例。

    流程：
    1. 解析模型名称（未指定时使用配置中的第一个模型）
    2. 通过 resolve_class 反射加载模型类
    3. 合并配置参数，处理 thinking 模式和 reasoning_effort
    4. 可选注入 LangSmith 追踪器

    kwargs 可含 reasoning_effort: str | None
    """
    ...


# ============================================================
# 工具系统 (004-tool-system)
# ============================================================

def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
) -> list[BaseTool]:
    """获取 Agent 可用的完整工具列表。

    Args:
        groups: 工具分组过滤列表，为 None 时加载全部。
        include_mcp: 是否包含 MCP 服务器提供的工具。
        model_name: 模型名称，用于判断是否应加载视觉工具。
        subagent_enabled: 是否包含子代理委派工具（task、task_status）。
    """
    ...


# ============================================================
# 提示词模板 (006-prompt-template)
# ============================================================

def apply_prompt_template(
    subagent_enabled: bool = False,
    max_concurrent_subagents: int = 3,
    *,
    agent_name: str | None = None,
    available_skills: set[str] | None = None,
) -> str:
    """应用系统提示词模板，组装完整的智能体系统提示词。

    动态组装包含角色定义、记忆上下文、技能列表、子智能体指令、
    工作目录说明、引用格式等内容的完整系统提示词。
    """
    ...


# ============================================================
# 中间件系统 (007-middleware-system)
# ============================================================

def build_lead_runtime_middlewares(*, lazy_init: bool = True) -> list:
    """主智能体运行时的基础中间件列表。

    返回: [ThreadDataMiddleware, UploadsMiddleware, SandboxMiddleware,
           DanglingToolCallMiddleware, ToolErrorHandlingMiddleware]
    """
    ...
