"""工具配置 — 工具声明与分组。

工具配置声明 Agent 可用的工具列表和逻辑分组。
每个工具通过 `use` 字段指向一个 Python 变量（如 deerflow.sandbox.tools:bash_tool），
由 reflection 模块在运行时解析为实际的 LangChain Tool 实例。

### 工具声明（ToolConfig）
- name: 工具的唯一名称
- group: 所属工具组（用于按组过滤）
- use: 工具提供者的 Python 路径（module.path:variable_name）
- extra="allow" 允许透传 Provider 特定参数

### 工具分组（ToolGroupConfig）
- name: 分组名称
- extra="allow" 允许分组级别的额外配置

工具分组用于在 Agent 运行时选择性地加载工具。
例如，子代理可以只加载特定分组的工具。
"""

from pydantic import BaseModel, ConfigDict, Field


class ToolGroupConfig(BaseModel):
    """工具分组配置。

    工具分组提供逻辑上的工具集合划分，用于：
    - 按组过滤 Agent 可用的工具
    - 子代理的工具继承控制
    """

    name: str = Field(..., description="Unique name for the tool group")
    model_config = ConfigDict(extra="allow")


class ToolConfig(BaseModel):
    """单个工具的配置。

    - name: 工具唯一标识（如 bash、read_file）
    - group: 所属分组名（如 sandbox、builtin）
    - use: 工具提供者的完整路径（如 deerflow.sandbox.tools:bash_tool）
      格式为 module.path:variable_name，由 reflection.resolve_variable() 解析
    """

    name: str = Field(..., description="Unique name for the tool")
    group: str = Field(..., description="Group name for the tool")
    use: str = Field(
        ...,
        description="Variable name of the tool provider(e.g. deerflow.sandbox.tools:bash_tool)",
    )
    model_config = ConfigDict(extra="allow")
