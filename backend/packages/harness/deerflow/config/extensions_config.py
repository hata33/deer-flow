"""统一扩展配置 — MCP 服务器和技能状态声明。

本模块管理 extensions_config.json 文件的加载和解析。
该文件声明所有 MCP 服务器和技能的启用状态。

### 为什么单独一个配置文件
MCP 服务器和技能状态需要频繁修改（通过 Gateway API），
与 config.yaml（主配置，手动编辑）分离：
- config.yaml: 静态配置（模型、沙箱、工具声明），手动维护
- extensions_config.json: 动态配置（MCP 服务器、技能开关），API 驱动

### 配置文件优先级
1. 显式 config_path 参数
2. DEER_FLOW_EXTENSIONS_CONFIG_PATH 环境变量
3. 项目根目录下的 extensions_config.json
4. 传统 mcp_config.json（向后兼容）
5. backend/ 和 repo-root 目录下的回退查找
6. 都找不到 → 返回空配置（扩展是可选的）

### 环境变量解析
配置值以 $ 开头的会被解析为环境变量（如 $OPENAI_API_KEY）。
解析在原地修改 dict（in-place），而非返回新 dict。
未找到的环境变量被替换为空字符串，避免下游收到字面的 $VAR。

### 全局单例 + 缓存
get_extensions_config() 返回缓存的单例。
Gateway API 修改配置后调用 reload_extensions_config() 刷新。
"""

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from deerflow.config.runtime_paths import existing_project_file


class McpOAuthConfig(BaseModel):
    """MCP 服务器的 OAuth 配置（HTTP/SSE 传输）。

    支持 client_credentials 和 refresh_token 两种授权类型。
    令牌端点的响应字段名可配置，适配非标准 OAuth 提供商。
    """

    enabled: bool = Field(default=True, description="Whether OAuth token injection is enabled")
    token_url: str = Field(description="OAuth token endpoint URL")
    grant_type: Literal["client_credentials", "refresh_token"] = Field(
        default="client_credentials",
        description="OAuth grant type",
    )
    client_id: str | None = Field(default=None, description="OAuth client ID")
    client_secret: str | None = Field(default=None, description="OAuth client secret")
    refresh_token: str | None = Field(default=None, description="OAuth refresh token (for refresh_token grant)")
    scope: str | None = Field(default=None, description="OAuth scope")
    audience: str | None = Field(default=None, description="OAuth audience (provider-specific)")
    token_field: str = Field(default="access_token", description="Field name containing access token in token response")
    token_type_field: str = Field(default="token_type", description="Field name containing token type in token response")
    expires_in_field: str = Field(default="expires_in", description="Field name containing expiry (seconds) in token response")
    default_token_type: str = Field(default="Bearer", description="Default token type when missing in token response")
    refresh_skew_seconds: int = Field(default=60, description="Refresh token this many seconds before expiry")
    extra_token_params: dict[str, str] = Field(default_factory=dict, description="Additional form params sent to token endpoint")
    model_config = ConfigDict(extra="allow")


class McpServerConfig(BaseModel):
    """单个 MCP 服务器的配置。

    ### 传输类型
    - stdio: 启动子进程通信（command + args + env）
    - sse: Server-Sent Events 连接（url + headers）
    - http: HTTP 长连接（url + headers）

    ### OAuth（sse/http 专用）
    通过 oauth 字段配置自动令牌获取和刷新。
    """

    enabled: bool = Field(default=True, description="Whether this MCP server is enabled")
    type: str = Field(default="stdio", description="Transport type: 'stdio', 'sse', or 'http'")
    command: str | None = Field(default=None, description="Command to execute to start the MCP server (for stdio type)")
    args: list[str] = Field(default_factory=list, description="Arguments to pass to the command (for stdio type)")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for the MCP server")
    url: str | None = Field(default=None, description="URL of the MCP server (for sse or http type)")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP headers to send (for sse or http type)")
    oauth: McpOAuthConfig | None = Field(default=None, description="OAuth configuration (for sse or http type)")
    description: str = Field(default="", description="Human-readable description of what this MCP server provides")
    model_config = ConfigDict(extra="allow")


class SkillStateConfig(BaseModel):
    """单个技能的启用状态。"""

    enabled: bool = Field(default=True, description="Whether this skill is enabled")


class ExtensionsConfig(BaseModel):
    """扩展配置聚合模型。

    - mcp_servers: MCP 服务器名 → 配置的映射（JSON 中使用 mcpServers 字段名）
    - skills: 技能名 → 启用状态的映射
    """

    mcp_servers: dict[str, McpServerConfig] = Field(
        default_factory=dict,
        description="Map of MCP server name to configuration",
        alias="mcpServers",
    )
    skills: dict[str, SkillStateConfig] = Field(
        default_factory=dict,
        description="Map of skill name to state configuration",
    )
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path | None:
        """解析扩展配置文件路径。

        优先级：
        1. 显式参数
        2. DEER_FLOW_EXTENSIONS_CONFIG_PATH 环境变量
        3. 项目根目录下的 extensions_config.json 或 mcp_config.json
        4. 传统 backend/repo-root 位置的回退查找
        5. 都找不到 → 返回 None（扩展是可选的）
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
            project_config = existing_project_file(("extensions_config.json", "mcp_config.json"))
            if project_config is not None:
                return project_config

            # 传统 monorepo 位置回退
            backend_dir = Path(__file__).resolve().parents[4]
            repo_root = backend_dir.parent
            for path in (
                backend_dir / "extensions_config.json",
                repo_root / "extensions_config.json",
                backend_dir / "mcp_config.json",
                repo_root / "mcp_config.json",
            ):
                if path.exists():
                    return path

            return None

    @classmethod
    def from_file(cls, config_path: str | None = None) -> "ExtensionsConfig":
        """从 JSON 文件加载扩展配置。

        文件不存在时返回空配置（扩展是可选的）。
        JSON 解析失败时抛出 ValueError。
        """
        resolved_path = cls.resolve_config_path(config_path)
        if resolved_path is None:
            return cls(mcp_servers={}, skills={})

        try:
            with open(resolved_path, encoding="utf-8") as f:
                config_data = json.load(f)
            cls.resolve_env_variables(config_data)
            return cls.model_validate(config_data)
        except json.JSONDecodeError as e:
            raise ValueError(f"Extensions config file at {resolved_path} is not valid JSON: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to load extensions config from {resolved_path}: {e}") from e

    @classmethod
    def resolve_env_variables(cls, config: dict[str, Any]) -> dict[str, Any]:
        """递归解析配置中的环境变量。

        值以 $ 开头 → 解析为环境变量值。
        未找到的环境变量 → 替换为空字符串（避免下游收到字面 $VAR）。

        注意：此方法原地修改 dict（in-place mutation），与 AppConfig 版本不同。
        """
        for key, value in config.items():
            if isinstance(value, str):
                if value.startswith("$"):
                    env_value = os.getenv(value[1:])
                    if env_value is None:
                        # 未解析的占位符 → 存储空字符串
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
        """获取所有启用的 MCP 服务器。"""
        return {name: config for name, config in self.mcp_servers.items() if config.enabled}

    def is_skill_enabled(self, skill_name: str, skill_category: str) -> bool:
        """检查技能是否启用。

        - 配置中有记录 → 使用配置值
        - 配置中无记录 → public 和 custom 类别默认启用
        """
        skill_config = self.skills.get(skill_name)
        if skill_config is None:
            return skill_category in ("public", "custom")
        return skill_config.enabled


# ── 全局单例管理 ──

_extensions_config: ExtensionsConfig | None = None


def get_extensions_config() -> ExtensionsConfig:
    """获取扩展配置（缓存单例）。

    首次调用时从文件加载，后续返回缓存。
    使用 reload_extensions_config() 强制刷新。
    """
    global _extensions_config
    if _extensions_config is None:
        _extensions_config = ExtensionsConfig.from_file()
    return _extensions_config


def reload_extensions_config(config_path: str | None = None) -> ExtensionsConfig:
    """重新从文件加载扩展配置并更新缓存。

    Gateway API 修改配置后调用此函数刷新。
    """
    global _extensions_config
    _extensions_config = ExtensionsConfig.from_file(config_path)
    return _extensions_config


def reset_extensions_config() -> None:
    """重置缓存的扩展配置。下次 get 时重新加载。"""
    global _extensions_config
    _extensions_config = None


def set_extensions_config(config: ExtensionsConfig) -> None:
    """设置自定义的扩展配置实例（用于测试注入）。"""
    global _extensions_config
    _extensions_config = config
