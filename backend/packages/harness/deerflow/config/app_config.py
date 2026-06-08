"""应用根配置 — DeerFlow 的配置中枢。

AppConfig 是整个配置系统的根模型，聚合所有子系统的配置：
- 模型（models）、工具（tools）、沙箱（sandbox）
- 子代理（subagents）、记忆（memory）、摘要（summarization）
- MCP 扩展（extensions）、技能（skills）
- 数据库（database）、运行事件（run_events）、Checkpointer
- Guardrails、循环检测、标题生成、token 追踪
- ACP 代理、自定义代理 API、Stream Bridge

### 配置文件: config.yaml
主配置文件为 YAML 格式，位于项目根目录。

### 加载流程
1. resolve_config_path() 定位文件
2. YAML 解析
3. 检查配置版本（与 config.example.yaml 比较）
4. 递归解析环境变量（$VAR 语法）
5. 应用数据库默认值
6. 单独加载 extensions_config.json（MCP + 技能状态）
7. Pydantic 校验
8. 分发到各子系统的全局单例

### 缓存与热更新
get_app_config() 返回缓存的单例，通过 mtime 比对自动检测文件变更：
- 文件被修改 → mtime 变更 → 自动重新加载
- 文件路径变更 → 重新加载
- 支持手动 reload_app_config() 和 reset_app_config()

### ContextVar 覆盖栈
push/pop_current_app_config() 提供协程安全的配置覆盖：
- 用于测试中注入临时配置
- LangGraph 运行时可以为不同线程使用不同配置
- 支持嵌套 push/pop（栈式管理）

### 环境变量解析
resolve_env_variables() 递归处理所有值：
- "$OPENAI_API_KEY" → os.getenv("OPENAI_API_KEY")
- 未找到时抛出 ValueError（配置错误应尽早发现）
"""

import logging
import os
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Self

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from deerflow.config.acp_config import ACPAgentConfig, load_acp_config_from_dict
from deerflow.config.agents_api_config import AgentsApiConfig, load_agents_api_config_from_dict
from deerflow.config.checkpointer_config import CheckpointerConfig, load_checkpointer_config_from_dict
from deerflow.config.database_config import DatabaseConfig
from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.config.guardrails_config import GuardrailsConfig, load_guardrails_config_from_dict
from deerflow.config.loop_detection_config import LoopDetectionConfig
from deerflow.config.memory_config import MemoryConfig, load_memory_config_from_dict
from deerflow.config.model_config import ModelConfig
from deerflow.config.run_events_config import RunEventsConfig
from deerflow.config.runtime_paths import existing_project_file
from deerflow.config.safety_finish_reason_config import SafetyFinishReasonConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.skill_evolution_config import SkillEvolutionConfig
from deerflow.config.skills_config import SkillsConfig
from deerflow.config.stream_bridge_config import StreamBridgeConfig, load_stream_bridge_config_from_dict
from deerflow.config.subagents_config import SubagentsAppConfig, load_subagents_config_from_dict
from deerflow.config.summarization_config import SummarizationConfig, load_summarization_config_from_dict
from deerflow.config.title_config import TitleConfig, load_title_config_from_dict
from deerflow.config.token_usage_config import TokenUsageConfig
from deerflow.config.tool_config import ToolConfig, ToolGroupConfig
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.config.tool_search_config import ToolSearchConfig, load_tool_search_config_from_dict

# 加载 .env 文件中的环境变量
load_dotenv()

logger = logging.getLogger(__name__)


# 当 config.yaml 中缺少 database 部分时使用的默认值
CONFIG_FILE_DATABASE_DEFAULTS = {
    "backend": "sqlite",
    "sqlite_dir": ".deer-flow/data",
}


class CircuitBreakerConfig(BaseModel):
    """LLM 熔断器配置。

    连续失败达到阈值后熔断，防止持续调用不可用的 LLM。
    recovery_timeout_sec 后尝试恢复。

    - failure_threshold: 触发熔断的连续失败次数
    - recovery_timeout_sec: 熔断后等待恢复的秒数
    """

    failure_threshold: int = Field(default=5, description="Number of consecutive failures before tripping the circuit")
    recovery_timeout_sec: int = Field(default=60, description="Time in seconds before attempting to recover the circuit")


def _legacy_config_candidates() -> tuple[Path, ...]:
    """返回传统 monorepo 中的 config.yaml 位置（向后兼容）。"""
    backend_dir = Path(__file__).resolve().parents[4]
    repo_root = backend_dir.parent
    return (backend_dir / "config.yaml", repo_root / "config.yaml")


def logging_level_from_config(name: str | None) -> int:
    """将配置中的日志级别字符串映射为 logging 模块的级别常量。"""
    mapping = logging.getLevelNamesMapping()
    return mapping.get((name or "info").strip().upper(), logging.INFO)


def apply_logging_level(name: str | None) -> None:
    """应用配置中的日志级别。

    只修改 deerflow 和 app 两个 logger 的级别，
    不影响第三方库（uvicorn、sqlalchemy 等）的日志输出。

    Root handler 级别只降低不升高，确保 deerflow/app 的消息能传播。
    """
    level = logging_level_from_config(name)
    for logger_name in ("deerflow", "app"):
        logging.getLogger(logger_name).setLevel(level)
    for handler in logging.root.handlers:
        if level < handler.level:
            handler.setLevel(level)


class AppConfig(BaseModel):
    """DeerFlow 应用根配置。

    聚合所有子系统的配置。extra="allow" 允许未识别的字段透传。

    ### 查询方法
    - get_model_config(name): 按名称查找模型配置
    - get_tool_config(name): 按名称查找工具配置
    - get_tool_group_config(name): 按名称查找工具组配置
    """

    log_level: str = Field(default="info", description="Logging level for deerflow and app modules (debug/info/warning/error); third-party libraries are not affected")
    token_usage: TokenUsageConfig = Field(default_factory=TokenUsageConfig, description="Token usage tracking configuration")
    models: list[ModelConfig] = Field(default_factory=list, description="Available models")
    sandbox: SandboxConfig = Field(description="Sandbox configuration")
    tools: list[ToolConfig] = Field(default_factory=list, description="Available tools")
    tool_groups: list[ToolGroupConfig] = Field(default_factory=list, description="Available tool groups")
    skills: SkillsConfig = Field(default_factory=SkillsConfig, description="Skills configuration")
    skill_evolution: SkillEvolutionConfig = Field(default_factory=SkillEvolutionConfig, description="Agent-managed skill evolution configuration")
    extensions: ExtensionsConfig = Field(default_factory=ExtensionsConfig, description="Extensions configuration (MCP servers and skills state)")
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig, description="Tool output budget protection configuration")
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig, description="Tool search / deferred loading configuration")
    title: TitleConfig = Field(default_factory=TitleConfig, description="Automatic title generation configuration")
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig, description="Conversation summarization configuration")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory subsystem configuration")
    agents_api: AgentsApiConfig = Field(default_factory=AgentsApiConfig, description="Custom-agent management API configuration")
    acp_agents: dict[str, ACPAgentConfig] = Field(default_factory=dict, description="ACP-compatible agent configuration")
    subagents: SubagentsAppConfig = Field(default_factory=SubagentsAppConfig, description="Subagent runtime configuration")
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig, description="Guardrail middleware configuration")
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig, description="LLM circuit breaker configuration")
    loop_detection: LoopDetectionConfig = Field(default_factory=LoopDetectionConfig, description="Loop detection middleware configuration")
    safety_finish_reason: SafetyFinishReasonConfig = Field(default_factory=SafetyFinishReasonConfig, description="Provider safety-filter finish_reason interception middleware configuration")
    model_config = ConfigDict(extra="allow")
    database: DatabaseConfig = Field(default_factory=DatabaseConfig, description="Unified database backend configuration")
    run_events: RunEventsConfig = Field(default_factory=RunEventsConfig, description="Run event storage configuration")
    checkpointer: CheckpointerConfig | None = Field(default=None, description="Checkpointer configuration")
    stream_bridge: StreamBridgeConfig | None = Field(default=None, description="Stream bridge configuration")

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path:
        """解析 config.yaml 文件路径。

        优先级：
        1. 显式参数
        2. DEER_FLOW_CONFIG_PATH 环境变量
        3. 项目根目录下的 config.yaml
        4. 传统 monorepo 位置回退
        5. 都找不到 → FileNotFoundError
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
            project_config = existing_project_file(("config.yaml",))
            if project_config is not None:
                return project_config

            for path in _legacy_config_candidates():
                if path.exists():
                    return path
            raise FileNotFoundError("`config.yaml` file not found in the project root or legacy backend/repository root locations")

    @classmethod
    def from_file(cls, config_path: str | None = None) -> Self:
        """从 YAML 文件加载配置。

        流程：
        1. 定位并读取 YAML 文件
        2. 检查配置版本
        3. 解析环境变量
        4. 应用数据库默认值
        5. 加载扩展配置（extensions_config.json）
        6. Pydantic 校验
        7. 分发到子系统全局单例
        """
        resolved_path = cls.resolve_config_path(config_path)
        with open(resolved_path, encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        # 检查配置版本，落后时发出警告
        cls._check_config_version(config_data, resolved_path)

        # 递归解析 $VAR 环境变量
        config_data = cls.resolve_env_variables(config_data)
        # 填充 database 部分的默认值
        cls._apply_database_defaults(config_data)

        # 加载 circuit_breaker 配置
        if "circuit_breaker" in config_data:
            config_data["circuit_breaker"] = config_data["circuit_breaker"]

        # 扩展配置从单独的 JSON 文件加载（支持 API 动态修改）
        extensions_config = ExtensionsConfig.from_file()
        config_data["extensions"] = extensions_config.model_dump()

        result = cls.model_validate(config_data)
        # 验证并处理 ACP 代理配置
        acp_agents = cls._validate_acp_agents(config_data.get("acp_agents", {}))
        # 将配置分发到各子系统的全局单例
        cls._apply_singleton_configs(result, acp_agents)
        return result

    @classmethod
    def _validate_acp_agents(
        cls,
        config_data: Mapping[str, Mapping[str, object]] | None,
    ) -> dict[str, ACPAgentConfig]:
        """验证并构建 ACP 代理配置字典。"""
        if config_data is None:
            config_data = {}
        return {name: ACPAgentConfig(**cfg) for name, cfg in config_data.items()}

    @classmethod
    def _apply_singleton_configs(cls, config: Self, acp_agents: dict[str, ACPAgentConfig]) -> None:
        """将配置分发到各子系统的全局单例。

        这种设计是为了兼容尚未迁移到显式 AppConfig 传递的代码路径。
        新代码应优先直接传递 AppConfig 实例。

        当 checkpointer 配置变更时，需要重置运行时的 checkpointer 和 store 实例，
        因为它们的后端类型取决于 checkpointer 配置。
        """
        from deerflow.config.checkpointer_config import get_checkpointer_config

        previous_checkpointer_config = get_checkpointer_config()

        # 分发到各子系统的全局单例
        load_title_config_from_dict(config.title.model_dump())
        load_summarization_config_from_dict(config.summarization.model_dump())
        load_memory_config_from_dict(config.memory.model_dump())
        load_agents_api_config_from_dict(config.agents_api.model_dump())
        load_subagents_config_from_dict(config.subagents.model_dump())
        load_tool_search_config_from_dict(config.tool_search.model_dump())
        load_guardrails_config_from_dict(config.guardrails.model_dump())
        load_checkpointer_config_from_dict(config.checkpointer.model_dump() if config.checkpointer is not None else None)
        load_stream_bridge_config_from_dict(config.stream_bridge.model_dump() if config.stream_bridge is not None else None)
        load_acp_config_from_dict({name: agent.model_dump() for name, agent in acp_agents.items()})

        if previous_checkpointer_config != config.checkpointer:
            # checkpointer 变更时需要重置依赖它的运行时单例
            from deerflow.runtime.checkpointer import reset_checkpointer
            from deerflow.runtime.store import reset_store

            reset_checkpointer()
            reset_store()

    @classmethod
    def _apply_database_defaults(cls, config_data: dict[str, Any]) -> None:
        """当 config.yaml 缺少 database 部分时填充默认值。"""
        database_config = config_data.get("database")
        if database_config is None:
            database_config = {}
            config_data["database"] = database_config
        if not isinstance(database_config, dict):
            return
        for key, value in CONFIG_FILE_DATABASE_DEFAULTS.items():
            database_config.setdefault(key, value)

    @classmethod
    def _check_config_version(cls, config_data: dict, config_path: Path) -> None:
        """检查用户的 config.yaml 版本是否过时。

        将用户版本与 config.example.yaml 的版本比较。
        缺少 config_version 字段视为版本 0（版本化之前的配置）。
        """
        try:
            user_version = int(config_data.get("config_version", 0))
        except (TypeError, ValueError):
            user_version = 0

        # 在 config.yaml 的目录及其上级目录查找 config.example.yaml
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
        """递归解析配置中的环境变量。

        遍历所有 dict、list、str 值：
        - "$OPENAI_API_KEY" → os.getenv("OPENAI_API_KEY")
        - 环境变量不存在 → ValueError（配置错误应尽早发现）

        注意：此方法返回新对象，不修改原始配置（纯函数）。
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
        """按名称查找模型配置。"""
        return next((model for model in self.models if model.name == name), None)

    def get_tool_config(self, name: str) -> ToolConfig | None:
        """按名称查找工具配置。"""
        return next((tool for tool in self.tools if tool.name == name), None)

    def get_tool_group_config(self, name: str) -> ToolGroupConfig | None:
        """按名称查找工具组配置。"""
        return next((group for group in self.tool_groups if group.name == name), None)


# ── 兼容性单例层 ──
# 为尚未迁移到显式 AppConfig 传递的代码路径提供全局访问。
# 新的组合根应优先构建一次 AppConfig 并直接传递。

_app_config: AppConfig | None = None
_app_config_path: Path | None = None
_app_config_mtime: float | None = None
_app_config_is_custom = False

# ContextVar 覆盖栈 — 协程安全的配置覆盖
_current_app_config: ContextVar[AppConfig | None] = ContextVar("deerflow_current_app_config", default=None)
_current_app_config_stack: ContextVar[tuple[AppConfig | None, ...]] = ContextVar("deerflow_current_app_config_stack", default=())


def _get_config_mtime(config_path: Path) -> float | None:
    """获取配置文件的修改时间（文件不存在时返回 None）。"""
    try:
        return config_path.stat().st_mtime
    except OSError:
        return None


def _load_and_cache_app_config(config_path: str | None = None) -> AppConfig:
    """从磁盘加载配置并更新缓存元数据。"""
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom

    resolved_path = AppConfig.resolve_config_path(config_path)
    _app_config = AppConfig.from_file(str(resolved_path))
    _app_config_path = resolved_path
    _app_config_mtime = _get_config_mtime(resolved_path)
    _app_config_is_custom = False
    return _app_config


def get_app_config() -> AppConfig:
    """获取 DeerFlow 配置实例（带缓存和自动热更新）。

    返回缓存的 AppConfig 单例，当检测到以下变化时自动重新加载：
    1. 配置文件路径变更
    2. 配置文件 mtime 变更

    ContextVar 覆盖优先级最高（用于测试和运行时配置切换）。

    自定义配置（通过 set_app_config 注入）不会被自动刷新。
    """
    global _app_config, _app_config_path, _app_config_mtime

    # ContextVar 覆盖优先
    runtime_override = _current_app_config.get()
    if runtime_override is not None:
        return runtime_override

    # 自定义配置不自动刷新
    if _app_config is not None and _app_config_is_custom:
        return _app_config

    resolved_path = AppConfig.resolve_config_path()
    current_mtime = _get_config_mtime(resolved_path)

    # 检测是否需要重新加载
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

    在手动修改 config.yaml 后调用。
    """
    return _load_and_cache_app_config(config_path)


def reset_app_config() -> None:
    """重置缓存的配置实例。

    下次 get_app_config() 将从文件重新加载。
    用于测试或切换配置时。
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom
    _app_config = None
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = False


def set_app_config(config: AppConfig) -> None:
    """注入自定义配置实例（用于测试）。

    自定义配置不会被 mtime 检测自动刷新。
    """
    global _app_config, _app_config_path, _app_config_mtime, _app_config_is_custom
    _app_config = config
    _app_config_path = None
    _app_config_mtime = None
    _app_config_is_custom = True


def peek_current_app_config() -> AppConfig | None:
    """查看当前 ContextVar 覆盖的配置（不触发加载）。"""
    return _current_app_config.get()


def push_current_app_config(config: AppConfig) -> None:
    """压入运行时配置覆盖（协程安全，支持嵌套）。

    将当前配置保存到栈中，设置新配置。
    用于测试中注入临时配置，或 LangGraph 运行时使用不同配置。
    """
    stack = _current_app_config_stack.get()
    _current_app_config_stack.set(stack + (_current_app_config.get(),))
    _current_app_config.set(config)


def pop_current_app_config() -> None:
    """弹出最新的运行时配置覆盖。

    恢复到上一个配置。栈为空时清除覆盖。
    """
    stack = _current_app_config_stack.get()
    if not stack:
        _current_app_config.set(None)
        return
    previous = stack[-1]
    _current_app_config_stack.set(stack[:-1])
    _current_app_config.set(previous)
