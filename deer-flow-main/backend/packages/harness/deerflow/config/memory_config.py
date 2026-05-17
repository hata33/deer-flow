"""记忆系统配置。

本模块定义了 DeerFlow 记忆系统的配置参数。
记忆系统通过 LLM 从对话中提取用户上下文和事实，并在后续对话中注入到系统提示词中。

记忆系统工作流：
    1. MemoryMiddleware 过滤对话消息（用户输入 + 最终 AI 回复）
    2. 将对话加入防抖队列
    3. 后台线程调用 LLM 提取上下文更新和事实
    4. 原子写入到存储文件（临时文件 + 重命名）
    5. 下次交互时将事实和上下文注入到系统提示词的 <memory> 标签中

配置参数说明：
    - **存储相关** — enabled、storage_path、storage_class
    - **更新相关** — debounce_seconds、model_name、max_facts、fact_confidence_threshold
    - **注入相关** — injection_enabled、max_injection_tokens

配置示例（config.yaml）：
    ```yaml
    memory:
      enabled: true
      storage_path: ""
      debounce_seconds: 30
      model_name: null
      max_facts: 100
      fact_confidence_threshold: 0.7
      injection_enabled: true
      max_injection_tokens: 2000
    ```

存储路径解析：
    - 空字符串 → 默认为 ``{base_dir}/memory.json``
    - 绝对路径 → 直接使用
    - 相对路径 → 相对于 ``Paths.base_dir`` 解析（不是后端工作目录）
"""
from pydantic import BaseModel, Field


class MemoryConfig(BaseModel):
    """记忆系统配置。

    Attributes:
        enabled: 是否启用记忆系统。
        storage_path: 记忆数据存储路径。
            空字符串 → 默认为 {base_dir}/memory.json；
            绝对路径 → 直接使用；
            相对路径 → 相对于 Paths.base_dir 解析。
        storage_class: 记忆存储提供者的类路径。
        debounce_seconds: 处理队列更新前的等待时间（秒），用于防抖合并多次更新。
        model_name: 记忆更新使用的模型名称。None 表示使用默认模型。
        max_facts: 存储的最大事实数量。
        fact_confidence_threshold: 事实存储的最低置信度阈值。
        injection_enabled: 是否将记忆注入到系统提示词中。
        max_injection_tokens: 记忆注入的最大 token 数。
    """

    enabled: bool = Field(
        default=True,
        description="是否启用记忆系统",
    )
    storage_path: str = Field(
        default="",
        description=(
            "记忆数据存储路径。"
            "空字符串默认为 {base_dir}/memory.json。"
            "绝对路径直接使用。"
            "相对路径相对于 Paths.base_dir 解析（不是后端工作目录）。"
            "注意：如果之前设置为 .deer-flow/memory.json，"
            "现在会解析为 {base_dir}/.deer-flow/memory.json；"
            "请迁移现有数据或使用绝对路径保留旧位置。"
        ),
    )
    storage_class: str = Field(
        default="deerflow.agents.memory.storage.FileMemoryStorage",
        description="记忆存储提供者的类路径",
    )
    debounce_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="处理队列更新前的等待时间（秒），用于防抖合并多次更新",
    )
    model_name: str | None = Field(
        default=None,
        description="记忆更新使用的模型名称（None = 使用默认模型）",
    )
    max_facts: int = Field(
        default=100,
        ge=10,
        le=500,
        description="存储的最大事实数量",
    )
    fact_confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="事实存储的最低置信度阈值",
    )
    injection_enabled: bool = Field(
        default=True,
        description="是否将记忆注入到系统提示词中",
    )
    max_injection_tokens: int = Field(
        default=2000,
        ge=100,
        le=8000,
        description="记忆注入的最大 token 数",
    )


# ── 全局配置实例 ──────────────────────────────────────────────────────────
_memory_config: MemoryConfig = MemoryConfig()


def get_memory_config() -> MemoryConfig:
    """获取当前记忆配置。"""
    return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    """直接设置记忆配置。"""
    global _memory_config
    _memory_config = config


def load_memory_config_from_dict(config_dict: dict) -> None:
    """从字典加载记忆配置（由 AppConfig.from_file 调用）。"""
    global _memory_config
    _memory_config = MemoryConfig(**config_dict)
