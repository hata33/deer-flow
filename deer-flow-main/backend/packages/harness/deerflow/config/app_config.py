"""应用主配置（AppConfig）——所有配置的根入口。

本模块定义了 DeerFlow 应用的核心配置类 AppConfig 及其加载、缓存和热重载机制。

配置层次结构：
    AppConfig 是配置树的根节点，包含以下子配置：
    - models[]      → ModelConfig       — 可用的 LLM 模型列表
    - tools[]       → ToolConfig        — 可用的工具列表
    - tool_groups[] → ToolGroupConfig   — 工具分组定义
    - sandbox       → SandboxConfig     — 沙箱执行环境配置
    - skills        → SkillsConfig      — 技能路径配置
    - extensions    → ExtensionsConfig  — MCP 服务器 + 技能状态（独立 JSON 文件）
    - token_usage   → TokenUsageConfig  — Token 用量追踪
    - tool_search   → ToolSearchConfig  — 延迟工具加载
    - checkpointer  → CheckpointerConfig | None — 状态持久化
    - stream_bridge → StreamBridgeConfig | None — 流桥接

    以下子配置通过全局单例管理（不在 AppConfig 中）：
    - title         → TitleConfig       — 自动标题生成
    - summarization → SummarizationConfig — 对话摘要
    - memory        → MemoryConfig      — 记忆系统
    - subagents     → SubagentsAppConfig — 子智能体
    - guardrails    → GuardrailsConfig  — 工具调用前置授权
    - acp_agents    → dict[str, ACPAgentConfig] — ACP 代理

配置文件加载流程：
    1. resolve_config_path() 按优先级查找 config.yaml
    2. from_file() 读取 YAML 并解析
    3. _check_config_version() 比较用户版本和示例版本
    4. resolve_env_variables() 递归解析 $ 前缀的环境变量
    5. 依次加载各子配置的全局单例
    6. ExtensionsConfig 从独立的 extensions_config.json 加载
    7. Pydantic model_validate() 验证完整配置

缓存与热重载：
    - get_app_config() 返回缓存的单例实例
    - 自动检测文件路径或 mtime 变化并重新加载
    - reload_app_config() 强制重新加载
    - reset_app_config() 清除缓存（用于测试）
    - set_app_config() 注入自定义实例（用于测试）
"""
import logging
import os
from pathlib import Path
from typing import Any, Self

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from deerflow.config.acp_config import load_acp_config_from_dict
from deerflow.config.checkpointer_config import CheckpointerConfig, load_checkpointer_config_from_dict
from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.config.guardrails_config import load_guardrails_config_from_dict
from deerflow.config.memory_config import load_memory_config_from_dict
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.skills_config import SkillsConfig
from deerflow.config.stream_bridge_config import StreamBridgeConfig, load_stream_bridge_config_from_dict
from deerflow.config.subagents_config import load_subagents_config_from_dict
from deerflow.config.summarization_config import load_summarization_config_from_dict
from deerflow.config.title_config import load_title_config_from_dict
from deerflow.config.token_usage_config import TokenUsageConfig
from deerflow.config.tool_config import ToolConfig, ToolGroupConfig
from deerflow.config.tool_search_config import ToolSearchConfig, load_tool_search_config_from_dict

# 加载 .env 文件中的环境变量（在 import 时执行）
load_dotenv()

logger = logging.getLogger(__name__)


class AppConfig(BaseModel):
    """DeerFlow 应用配置根类。

    对应 config.yaml 的完整结构。使用 Pydantic BaseModel 进行类型验证。
    extra="allow" 允许传入额外的未知字段（向前兼容）。
    frozen=False 允许运行时修改配置对象。

    Attributes:
        log_level: DeerFlow 模块的日志级别（debug/info/warning/error）。
        token_usage: Token 用量追踪配置。
        models: 可用的 LLM 模型列表。
        sandbox: 沙箱执行环境配置（必填）。
        tools: 可用的工具列表。
        tool_groups: 工具分组定义。
        skills: 技能路径配置。
        extensions: MCP 服务器和技能状态配置（从独立 JSON 文件加载）。
        tool_search: 延迟工具加载配置。
        checkpointer: LangGraph 状态持久化配置（可选）。
        stream_bridge: 流桥接配置（可选）。
    """

    log_level: str = Field(default="info", description="Logging level for deerflow modules (debug/info/warning/error)")
    token_usage: TokenUsageConfig = Field(default_factory=TokenUsageConfig, description="Token usage tracking configuration")
    models: list[ModelConfig] = Field(default_factory=list, description="Available models")
    sandbox: SandboxConfig = Field(description="Sandbox configuration")
    tools: list[ToolConfig] = Field(default_factory=list, description="Available tools")
    tool_groups: list[ToolGroupConfig] = Field(default_factory=list, description="Available tool groups")
    skills: SkillsConfig = Field(default_factory=SkillsConfig, description="Skills configuration")
    extensions: ExtensionsConfig = Field(default_factory=ExtensionsConfig, description="Extensions configuration (MCP servers and skills state)")
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig, description="Tool search / deferred loading configuration")
    # 允许传入额外的未知字段（向前兼容）；允许运行时修改
    model_config = ConfigDict(extra="allow", frozen=False)
    checkpointer: CheckpointerConfig | None = Field(default=None, description="Checkpointer configuration")
    stream_bridge: StreamBridgeConfig | None = Field(default=None, description="Stream bridge configuration")

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path:
        """解析配置文件路径。

        按以下优先级查找 config.yaml：
        1. 显式传入的 config_path 参数
        2. DEER_FLOW_CONFIG_PATH 环境变量
        3. 当前目录下的 config.yaml（通常是 backend/）
        4. 父目录下的 config.yaml（通常是项目根目录——推荐位置）

        Args:
            config_path: 可选的配置文件路径。

        Returns:
            解析后的配置文件绝对路径。

        Raises:
            FileNotFoundError: 配置文件不存在。
        """
        if config_path:
            path = Path(config_path)
            if not Path.exists(path):
                raise FileNotFoundError(f"Config file specified by param `config_path` not found at {path}")
            return path
        elif os.getenv("DEER_FLOW_CONFIG_PATH"):
            path = Path(os.getenv("DEER_FLOW_CONFIG_PATH"))
            if not Path.exists(path):
                raise FileNotFoundError(f"Config file specified by environment variable `DEER_FLOW_CONFIG_PATH` not found at {path}")
            return path
        else:
            # 优先检查当前目录
            path = Path(os.getcwd()) / "config.yaml"
            if not path.exists():
                # 回退到父目录（项目根目录——推荐位置）
                path = Path(os.getcwd()).parent / "config.yaml"
                if not path.exists():
                    raise FileNotFoundError("`config.yaml` file not found at the current directory nor its parent directory")
            return path

    @classmethod
    def from_file(cls, config_path: str | None = None) -> Self:
        """从 YAML 文件加载配置。

        加载流程：
        1. 解析配置文件路径
        2. 检查配置版本（与 config.example.yaml 比较）
        3. 递归解析 $ 前缀的环境变量
        4. 加载各子配置到全局单例（title、summarization、memory 等）
        5. 从独立 JSON 文件加载扩展配置
        6. Pydantic 验证并返回

        Args:
            config_path: 可选的配置文件路径。参见 resolve_config_path。

        Returns:
            加载并验证后的 AppConfig 实例。
        """
        resolved_path = cls.resolve_config_path(config_path)
        with open(resolved_path, encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        # 检查配置版本，如果用户的 config.yaml 过旧则发出警告
        cls._check_config_version(config_data, resolved_path)

        # 递归解析所有以 $ 开头的环境变量引用
        config_data = cls.resolve_env_variables(config_data)

        # ── 加载子配置到各自的全局单例 ────────────────────────────────────

        # 自动标题生成配置
        if "title" in config_data:
            load_title_config_from_dict(config_data["title"])

        # 对话摘要配置
        if "summarization" in config_data:
            load_summarization_config_from_dict(config_data["summarization"])

        # 记忆系统配置
        if "memory" in config_data:
            load_memory_config_from_dict(config_data["memory"])

        # 子智能体系统配置
        if "subagents" in config_data:
            load_subagents_config_from_dict(config_data["subagents"])

        # 延迟工具加载配置
        if "tool_search" in config_data:
            load_tool_search_config_from_dict(config_data["tool_search"])

        # 工具调用前置授权配置
        if "guardrails" in config_data:
            load_guardrails_config_from_dict(config_data["guardrails"])

        # LangGraph 状态持久化配置
        if "checkpointer" in config_data:
            load_checkpointer_config_from_dict(config_data["checkpointer"])

        # 流桥接配置
        if "stream_bridge" in config_data:
            load_stream_bridge_config_from_dict(config_data["stream_bridge"])

        # ACP 代理配置——每次都刷新，确保移除的条目不会残留
        load_acp_config_from_dict(config_data.get("acp_agents", {}))

        # 扩展配置从独立的 extensions_config.json 加载（MCP 服务器 + 技能状态）
        extensions_config = ExtensionsConfig.from_file()
        config_data["extensions"] = extensions_config.model_dump()

        # Pydantic 验证并返回
        result = cls.model_validate(config_data)
        return result

    @classmethod
    def _check_config_version(cls, config_data: dict, config_path: Path) -> None:
        """检查用户的 config.yaml 是否过时。

        将用户配置中的 config_version 与 config.example.yaml 中的比较。
        如果用户版本较低，发出警告提示运行 ``make config-upgrade``。
        缺少 config_version 字段视为版本 0（版本管理前的配置）。
        """
        try:
            user_version = int(config_data.get("config_version", 0))
        except (TypeError, ValueError):
            user_version = 0

        # 从 config.yaml 的目录向上查找 config.example.yaml（最多 5 层）
        example_path = None
        search_dir = config_path.parent
        for _ in range(5):
            candidate = search_dir / "config.example.yaml"
            if candidate.exists():
                example_path = candidate
                break
            parent = search_dir.parent
            if parent == search_dir:
                break
            search_dir = parent
        if example_path is None:
            return

        try:
            with open(example_path, encoding="utf-8") as f:
                example_data = yaml.safe_load(f)
            raw = example_data.get("config_version", 0) if example_data else 0
            try:
                example_version = int(raw)
            except (TypeError, ValueError):
                example_version = 0
        except Exception:
            return

        if user_version < example_version:
            logger.warning(
                "Your config.yaml (version %d) is outdated — the latest version is %d. Run `make config-upgrade` to merge new fields into your config.",
                user_version,
                example_version,
            )

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        """递归解析配置中的环境变量引用。

        所有以 ``$`` 开头的字符串值会被替换为对应的环境变量值。
        例如 ``$OPENAI_API_KEY`` → 实际的 API Key 值。

        支持嵌套的字典和列表结构。

        Args:
            config: 待解析的配置值（可以是字符串、字典、列表或原始值）。

        Returns:
            解析后的配置值。

        Raises:
            ValueError: 环境变量不存在。
        """
        if isinstance(config, str):
            if config.startswith("$"):
                env_value = os.getenv(config[1:])
                if env_value is None:
                    raise ValueError(f"Environment variable {config[1:]} not found for config value {config}")
                return env_value
            return config
        elif isinstance(config, dict):
            return {k: cls.resolve_env_variables(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        return config

    def get_model_config(self, name: str) -> ModelConfig | None:
        """按名称查找模型配置。

        Args:
            name: 模型标识名（对应 ModelConfig.name）。

        Returns:
            匹配的 ModelConfig，未找到返回 None。
        """
        return next((model for model in self.models if model.name == name), None)

    def get_tool_config(self, name: str) -> ToolConfig | None:
        """按名称查找工具配置。

        Args:
            name: 工具标识名（对应 ToolConfig.name）。

        Returns:
            匹配的 ToolConfig，未找到返回 None。
        """
        return next((tool for tool in self.tools if tool.name == name), None)

    def get_tool_group_config(self, name: str) -> ToolGroupConfig | None:
        """按名称查找工具组配置。

        Args:
            name: 工具组标识名（对应 ToolGroupConfig.name）。

        Returns:
            匹配的 ToolGroupConfig，未找到返回 None。
        """
        return next((group for group in self.tool_groups if group.name == name), None)


# ── 全局缓存与热重载 ─────────────────────────────────────────────────────
# 通过文件路径和 mtime 变化检测实现自动热重载，无需重启应用。

_app_config: AppConfig | None = None       # 缓存的配置单例
_app_config_path: Path | None = None       # 当前加载的配置文件路径
_app_config_mtime: float | None = None     # 当前配置文件的修改时间
_app_config_is_custom = False              # 是否通过 set_app_config() 注入的自定义实例


def _get_config_mtime(config_path: Path) -> float | None:
    """获取配置文件的修改时间（mtime），文件不存在时返回 None。"""
    try:
        return config_path.stat().st_mtime
    except OSError:
        return None


def _load_and_cache_app_config(config_path: str | None = None) -> AppConfig:
    """从磁盘加载配置并刷新缓存元数据。

    这是实际的 I/O 操作函数，加载后会更新全局缓存变量。
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom

    resolved_path = AppConfig.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _app_config_path = resolved_path
    _app_config_mtime = _get_config_mtime(resolved_path)
    _app_config_is_custom = False
    return _app_config


def get_app_config() -> AppConfig:
    """获取 AppConfig 单例实例。

    返回缓存的实例，并在以下条件触发自动重载：
    - 配置文件路径发生变化
    - 配置文件的 mtime 发生变化

    通过 set_app_config() 注入的自定义实例不会自动重载。

    使用 reload_app_config() 强制重载，或 reset_app_config() 清除缓存。
    """
    global _app_config, _app_config_path, _app_config_mtime

    # 自定义实例（如测试中注入的 mock）不做自动重载
    if _app_config is not None and _app_config_is_custom:
        return _app_config

    resolved_path = AppConfig.resolve_config_path()
    current_mtime = _get_config_mtime(resolved_path)

    # 检测是否需要重新加载：首次加载、路径变化或文件修改
    should_reload = _app_config is None or _app_config_path != resolved_path or _app_config_mtime != current_mtime
    if should_reload:
        if _app_config_path == resolved_path and _app_config_mtime is not None and current_mtime is not None and _app_config_mtime != current_mtime:
            logger.info(
                "Config file has been modified (mtime: %s -> %s), reloading AppConfig",
                _app_config_mtime,
                current_mtime,
            )
        _load_and_cache_app_config(str(resolved_path))
    return _app_config


def reload_app_config(config_path: str | None = None) -> AppConfig:
    """强制从文件重新加载配置并更新缓存。

    适用于配置文件被手动修改后希望立即生效的场景。

    Args:
        config_path: 可选的配置文件路径。未提供时使用默认解析策略。

    Returns:
        新加载的 AppConfig 实例。
    """
    return _load_and_cache_app_config(config_path)


def reset_app_config() -> None:
    """清除缓存的配置实例。

    下次调用 get_app_config() 时会重新从文件加载。
    主要用于测试或在不同配置间切换时。
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom
    _app_config = None
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = False


def set_app_config(config: AppConfig) -> None:
    """注入自定义的配置实例。

    主要用于测试，注入 mock 或预构建的配置。
    注入后 get_app_config() 不会自动重载，直到调用 reset_app_config()。

    Args:
        config: 要使用的 AppConfig 实例。
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom
    _app_config = config
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = True
