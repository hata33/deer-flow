"""工具和工具分组配置定义。

本模块定义了 DeerFlow 工具系统的配置结构。
工具是代理可以调用的能力单元，通过反射系统（resolve_variable）动态加载。

核心概念：
    - **工具（Tool）** — 代理可调用的具体能力（如 bash、read_file）。
    - **工具组（Tool Group）** — 将多个工具按逻辑分组，便于管理和分配。
    - **use 路径** — 工具提供者的变量路径，通过 resolve_variable() 动态加载。

工具加载流程：
    1. 从 config.yaml 读取 tools[] 和 tool_groups[] 配置。
    2. 通过 resolve_variable(use) 动态加载工具提供者。
    3. 工具提供者可以是函数、类实例或 BaseTool 对象。
    4. get_available_tools() 组合所有来源的工具（配置、MCP、内置、子智能体）。

配置示例（config.yaml）：
    ```yaml
    tools:
      - name: bash
        group: default
        use: deerflow.sandbox.tools:bash_tool
      - name: web_search
        group: search
        use: deerflow.community.tavily:tavily_search_tool

    tool_groups:
      - name: default
      - name: search
    ```

注意：
    - model_config = ConfigDict(extra="allow") 允许传入额外的提供商特定参数。
"""
from pydantic import BaseModel, ConfigDict, Field


class ToolGroupConfig(BaseModel):
    """工具分组配置。

    工具组用于将工具按逻辑分组，便于在智能体配置中按组分配工具。

    Attributes:
        name: 工具组的唯一名称。
    """

    name: str = Field(..., description="工具组的唯一名称")
    # 允许传入额外的分组元数据
    model_config = ConfigDict(extra="allow")


class ToolConfig(BaseModel):
    """工具配置。

    对应 config.yaml 中 tools[] 列表的单个工具定义。

    Attributes:
        name: 工具的唯一名称（用于在运行时标识工具）。
        group: 工具所属的分组名称（对应 ToolGroupConfig.name）。
        use: 工具提供者的变量路径，通过 resolve_variable() 动态加载。
            格式为 ``module.path:variable_name``，
            如 ``deerflow.sandbox.tools:bash_tool``。
    """

    name: str = Field(..., description="工具的唯一名称")
    group: str = Field(..., description="工具所属的分组名称")
    use: str = Field(
        ...,
        description="工具提供者的变量路径（如 deerflow.sandbox.tools:bash_tool）",
    )
    # 允许传入额外的工具特定参数
    model_config = ConfigDict(extra="allow")
