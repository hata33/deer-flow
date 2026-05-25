"""DeerFlow 工具系统（Tools Package）

本模块是 DeerFlow 工具系统的入口，负责对外暴露工具注册与查询接口。

工具系统的核心职责：
-------------------
1. **工具装配（Tool Assembly）**
   通过 `get_available_tools()` 函数，按优先级顺序装配所有可用工具：
   - 配置文件工具（config.yaml）→ MCP 工具 → 内置工具 → 子代理工具
   - 相同名称的工具，高优先级的优先保留，低优先级的被去重丢弃

2. **延迟加载（Deferred Loading）**
   `skill_manage_tool` 使用 `__getattr__` 实现延迟导入，
   避免模块加载时产生不必要的依赖开销。

3. **工具分类**
   - **配置工具**：通过 config.yaml 中的 `tools` 字段注册，由 `resolve_variable` 解析
   - **MCP 工具**：通过 MCP（Model Context Protocol）服务器提供，启动时缓存
   - **内置工具**：present_files、ask_clarification、view_image 等
   - **子代理工具**：task（任务委派），仅在启用子代理时可用

模块结构：
---------
- `tools.py`     — 工具装配管线（get_available_tools）
- `sync.py`      — 异步→同步桥接包装器
- `types.py`     — Runtime 类型别名定义
- `skill_manage_tool.py` — 自定义技能管理工具
- `builtins/`    — 内置工具集合

使用方式：
---------
    from deerflow.tools import get_available_tools

    tools = get_available_tools(
        groups=None,          # 可选：按工具组过滤
        include_mcp=True,     # 是否包含 MCP 工具
        model_name="gpt-4o",  # 模型名称（决定是否启用视觉工具）
        subagent_enabled=True # 是否启用子代理工具
    )
"""

from .tools import get_available_tools

__all__ = ["get_available_tools", "skill_manage_tool"]


def __getattr__(name: str):
    """延迟导入 skill_manage_tool，避免模块加载时的循环依赖。

    当首次访问 `deerflow.tools.skill_manage_tool` 时才执行实际导入，
    这可以避免在模块初始化阶段加载技能管理相关的重量级依赖。
    """
    if name == "skill_manage_tool":
        from .skill_manage_tool import skill_manage_tool

        return skill_manage_tool
    raise AttributeError(name)
