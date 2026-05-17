"""MCP 服务器和技能状态的统一扩展配置。

本模块定义了 DeerFlow 的扩展配置系统，统一管理 MCP（Model Context Protocol）服务器
和技能（Skills）的启用/禁用状态。

配置来源：
    从独立的 ``extensions_config.json`` 文件加载（不包含在 config.yaml 中），
    支持向后兼容旧的 ``mcp_config.json`` 文件名。

核心数据结构：
    - **McpServerConfig** — 单个 MCP 服务器的连接配置（stdio/sse/http 三种传输方式）。
    - **McpOAuthConfig** — MCP 服务器的 OAuth 认证配置（用于 HTTP/SSE 传输）。
    - **SkillStateConfig** — 单个技能的启用/禁用状态。
    - **ExtensionsConfig** — 顶层容器，包含 mcp_servers 和 skills 两个映射。

MCP 传输方式：
    - **stdio** — 通过标准输入/输出与子进程通信（command + args）。
    - **sse** — 通过 Server-Sent Events 连接远程服务器（url + headers）。
    - **http** — 通过 HTTP 请求连接远程服务器（url + headers）。

OAuth 认证流程：
    当 MCP 服务器配置了 OAuth 时，系统会自动：
    1. 使用 client_credentials 或 refresh_token 授权类型获取访问令牌。
    2. 在请求头中注入 ``Authorization: Bearer <token>``。
    3. 在令牌过期前自动刷新（refresh_skew_seconds 控制提前量）。

环境变量解析：
    配置值中以 ``$`` 开头的字符串会从宿主机环境变量解析。
    无法解析的环境变量会被替换为空字符串，避免将 ``$VAR`` 原样传递给下游。

配置示例（extensions_config.json）：
    ```json
    {
      "mcpServers": {
        "filesystem": {
          "enabled": true,
          "type": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
          "description": "File system access"
        },
        "remote-api": {
          "type": "http",
          "url": "https://api.example.com/mcp",
          "oauth": {
            "token_url": "https://auth.example.com/token",
            "client_id": "my-client",
            "client_secret": "$OAUTH_SECRET"
          }
        }
      },
      "skills": {
        "web-search": { "enabled": true }
      }
    }
    ```

全局实例管理：
    - get_extensions_config() — 获取缓存的单例
    - reload_extensions_config() — 强制重新加载
    - reset_extensions_config() — 清除缓存（用于测试）
    - set_extensions_config() — 注入自定义实例（用于测试）
"""
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class McpOAuthConfig(BaseModel):
    """MCP 服务器的 OAuth 认证配置（适用于 HTTP/SSE 传输）。

    支持两种授权类型：
    - **client_credentials** — 客户端凭据授权（适用于服务间通信）。
    - **refresh_token** — 刷新令牌授权（适用于需要用户上下文的场景）。

    令牌自动刷新：
        在令牌过期前 refresh_skew_seconds 秒自动刷新，
        确保请求不会因令牌过期而失败。
    """

    enabled: bool = Field(default=True, description="是否启用 OAuth 令牌注入")
    token_url: str = Field(description="OAuth 令牌端点 URL")
    grant_type: Literal["client_credentials", "refresh_token"] = Field(
        default="client_credentials",
        description="OAuth 授权类型",
    )
    client_id: str | None = Field(default=None, description="OAuth 客户端 ID")
    client_secret: str | None = Field(default=None, description="OAuth 客户端密钥")
    refresh_token: str | None = Field(default=None, description="OAuth 刷新令牌（用于 refresh_token 授权类型）")
    scope: str | None = Field(default=None, description="OAuth 权限范围")
    audience: str | None = Field(default=None, description="OAuth 受众（提供商特定）")
    token_field: str = Field(default="access_token", description="令牌响应中包含访问令牌的字段名")
    token_type_field: str = Field(default="token_type", description="令牌响应中包含令牌类型的字段名")
    expires_in_field: str = Field(default="expires_in", description="令牌响应中包含过期时间（秒）的字段名")
    default_token_type: str = Field(default="Bearer", description="令牌类型缺失时的默认值")
    refresh_skew_seconds: int = Field(
        default=60,
        description="在令牌过期前多少秒触发刷新",
    )
    extra_token_params: dict[str, str] = Field(
        default_factory=dict,
        description="发送到令牌端点的额外表单参数",
    )
    model_config = ConfigDict(extra="allow")


class McpServerConfig(BaseModel):
    """单个 MCP 服务器的配置。

    支持三种传输方式：
    - **stdio** — 本地子进程通信，需要 command 和 args。
    - **sse** — Server-Sent Events 远程连接，需要 url。
    - **http** — HTTP 远程连接，需要 url。

    Attributes:
        enabled: 是否启用此 MCP 服务器。
        type: 传输类型（'stdio'、'sse' 或 'http'）。
        command: 启动 MCP 服务器的命令（stdio 类型）。
        args: 命令参数列表（stdio 类型）。
        env: 注入的环境变量。
        url: MCP 服务器 URL（sse 或 http 类型）。
        headers: HTTP 请求头（sse 或 http 类型）。
        oauth: OAuth 认证配置（sse 或 http 类型）。
        description: 人类可读的服务器描述。
    """

    enabled: bool = Field(default=True, description="是否启用此 MCP 服务器")
    type: str = Field(default="stdio", description="传输类型: 'stdio'、'sse' 或 'http'")
    command: str | None = Field(default=None, description="启动 MCP 服务器的命令（stdio 类型）")
    args: list[str] = Field(default_factory=list, description="命令参数列表（stdio 类型）")
    env: dict[str, str] = Field(default_factory=dict, description="MCP 服务器的环境变量")
    url: str | None = Field(default=None, description="MCP 服务器 URL（sse 或 http 类型）")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP 请求头（sse 或 http 类型）")
    oauth: McpOAuthConfig | None = Field(default=None, description="OAuth 认证配置（sse 或 http 类型）")
    description: str = Field(default="", description="MCP 服务器功能描述")
    model_config = ConfigDict(extra="allow")


class SkillStateConfig(BaseModel):
    """单个技能的启用/禁用状态。"""

    enabled: bool = Field(default=True, description="是否启用此技能")


class ExtensionsConfig(BaseModel):
    """MCP 服务器和技能的统一配置容器。

    Attributes:
        mcp_servers: MCP 服务器名称到配置的映射（JSON 中使用 mcpServers 作为键名）。
        skills: 技能名称到状态的映射。
    """

    mcp_servers: dict[str, McpServerConfig] = Field(
        default_factory=dict,
        description="MCP 服务器名称到配置的映射",
        alias="mcpServers",
    )
    skills: dict[str, SkillStateConfig] = Field(
        default_factory=dict,
        description="技能名称到状态的映射",
    )
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path | None:
        """解析扩展配置文件路径。

        按以下优先级查找：
        1. 显式传入的 config_path 参数
        2. DEER_FLOW_EXTENSIONS_CONFIG_PATH 环境变量
        3. 当前目录下的 extensions_config.json
        4. 父目录下的 extensions_config.json
        5. 当前目录下的 mcp_config.json（向后兼容）
        6. 父目录下的 mcp_config.json（向后兼容）
        7. 未找到返回 None（扩展配置是可选的）

        Args:
            config_path: 可选的配置文件路径。

        Returns:
            配置文件路径，未找到返回 None。
        """
        if config_path:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"Extensions config file specified by param `config_path` not found at {path}")
            return path
        elif os.getenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH"):
            path = Path(os.getenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH"))
            if not path.exists():
                raise FileNotFoundError(f"Extensions config file specified by environment variable `DEER_FLOW_EXTENSIONS_CONFIG_PATH` not found at {path}")
            return path
        else:
            # 当前目录
            path = Path(os.getcwd()) / "extensions_config.json"
            if path.exists():
                return path

            # 父目录（项目根目录——推荐位置）
            path = Path(os.getcwd()).parent / "extensions_config.json"
            if path.exists():
                return path

            # 向后兼容：检查旧的 mcp_config.json
            path = Path(os.getcwd()) / "mcp_config.json"
            if path.exists():
                return path

            path = Path(os.getcwd()).parent / "mcp_config.json"
            if path.exists():
                return path

            # 扩展配置是可选的，未找到时返回 None
            return None

    @classmethod
    def from_file(cls, config_path: str | None = None) -> "ExtensionsConfig":
        """从 JSON 文件加载扩展配置。

        如果配置文件不存在，返回空配置（无 MCP 服务器、无技能状态）。
        参见 resolve_config_path 了解文件查找逻辑。

        Args:
            config_path: 可选的配置文件路径。

        Returns:
            加载后的 ExtensionsConfig 实例。

        Raises:
            ValueError: JSON 解析失败。
            RuntimeError: 其他加载错误。
        """
        resolved_path = cls.resolve_config_path(config_path)
        if resolved_path is None:
            # 文件未找到时返回空配置
            return cls(mcp_servers={}, skills={})

        try:
            with open(resolved_path, encoding="utf-8") as f:
                config_data = json.load(f)
            # 递归解析环境变量引用
            cls.resolve_env_variables(config_data)
            return cls.model_validate(config_data)
        except json.JSONDecodeError as e:
            raise ValueError(f"Extensions config file at {resolved_path} is not valid JSON: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to load extensions config from {resolved_path}: {e}") from e

    @classmethod
    def resolve_env_variables(cls, config: dict[str, Any]) -> dict[str, Any]:
        """递归解析配置中的环境变量引用。

        以 ``$`` 开头的字符串值会从宿主机环境变量解析。
        无法解析的环境变量被替换为空字符串，避免将 ``$VAR`` 原样传递给下游（如 MCP 服务器）。

        Args:
            config: 待解析的配置字典（原地修改）。

        Returns:
            解析后的配置字典。
        """
        for key, value in config.items():
            if isinstance(value, str):
                if value.startswith("$"):
                    env_value = os.getenv(value[1:])
                    if env_value is None:
                        # 未解析的占位符 → 空字符串，防止下游收到 "$VAR" 字面值
                        config[key] = ""
                    else:
                        config[key] = env_value
                else:
                    config[key] = value
            elif isinstance(value, dict):
                config[key] = cls.resolve_env_variables(value)
            elif isinstance(value, list):
                config[key] = [cls.resolve_env_variables(item) if isinstance(item, dict) else item for item in value]
        return config

    def get_enabled_mcp_servers(self) -> dict[str, McpServerConfig]:
        """获取所有已启用的 MCP 服务器。

        Returns:
            名称 → 配置的字典，仅包含 enabled=True 的服务器。
        """
        return {name: config for name, config in self.mcp_servers.items() if config.enabled}

    def is_skill_enabled(self, skill_name: str, skill_category: str) -> bool:
        """检查指定技能是否启用。

        如果技能未在配置中注册，则根据类别决定默认行为：
        - public / custom 类别默认启用
        - 其他类别默认禁用

        Args:
            skill_name: 技能名称。
            skill_category: 技能类别（public / custom）。

        Returns:
            True 表示启用。
        """
        skill_config = self.skills.get(skill_name)
        if skill_config is None:
            # 未注册的 public 和 custom 技能默认启用
            return skill_category in ("public", "custom")
        return skill_config.enabled


# ── 全局缓存 ──────────────────────────────────────────────────────────────

_extensions_config: ExtensionsConfig | None = None


def get_extensions_config() -> ExtensionsConfig:
    """获取缓存的扩展配置单例。

    首次调用时从文件加载，后续调用直接返回缓存。
    使用 reload_extensions_config() 强制重载，
    或 reset_extensions_config() 清除缓存。

    Returns:
        缓存的 ExtensionsConfig 实例。
    """
    global _extensions_config
    if _extensions_config is None:
        _extensions_config = ExtensionsConfig.from_file()
    return _extensions_config


def reload_extensions_config(config_path: str | None = None) -> ExtensionsConfig:
    """强制从文件重新加载扩展配置。

    适用于运行时通过 Gateway API 修改配置后需要立即生效的场景。

    Args:
        config_path: 可选的配置文件路径。未提供时使用默认解析策略。

    Returns:
        新加载的 ExtensionsConfig 实例。
    """
    global _extensions_config
    _extensions_config = ExtensionsConfig.from_file(config_path)
    return _extensions_config


def reset_extensions_config() -> None:
    """清除缓存的扩展配置实例。

    下次调用 get_extensions_config() 时会重新从文件加载。
    主要用于测试。
    """
    global _extensions_config
    _extensions_config = None


def set_extensions_config(config: ExtensionsConfig) -> None:
    """注入自定义的扩展配置实例。

    主要用于测试。

    Args:
        config: 要使用的 ExtensionsConfig 实例。
    """
    global _extensions_config
    _extensions_config = config
