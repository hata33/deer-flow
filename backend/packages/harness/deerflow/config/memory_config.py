"""记忆系统配置 — 用户画像与上下文记忆。

记忆系统从对话中自动提取用户信息（偏好、知识、行为模式等），
持久化存储并在后续对话中注入到系统提示词中。

### 核心功能
1. **事实提取**: LLM 从对话中提取结构化事实（偏好/知识/上下文/行为/目标）
2. **去重**: 基于内容的前后空白归一化进行事实去重
3. **原子写入**: temp 文件 + rename 确保写入安全
4. **防抖**: 可配置的更新间隔（默认 30 秒），避免频繁 LLM 调用
5. **注入**: 将 top N 条事实 + 上下文摘要注入到 Agent 的系统提示词

### 存储路径与用户隔离
- storage_path 为空: 使用按用户隔离的路径 {base_dir}/users/{user_id}/memory.json
- storage_path 为绝对路径: 所有用户共享同一文件（退出用户隔离）
- storage_path 为相对路径: 基于 Paths.base_dir 解析

### 全局单例
本配置作为全局单例管理，由 AppConfig 初始化时更新。
"""

from typing import Literal

from pydantic import BaseModel, Field


class MemoryConfig(BaseModel):
    """记忆系统配置。

    ### 主开关
    - enabled: 是否启用记忆提取
    - injection_enabled: 是否将记忆注入到系统提示词

    ### 存储
    - storage_path: 存储路径（空=按用户隔离，绝对=共享，相对=基于 base_dir）
    - storage_class: 存储实现类路径

    ### 更新控制
    - debounce_seconds: 防抖间隔（秒）
    - model_name: 记忆更新使用的模型（None = 默认模型）

    ### 容量限制
    - max_facts: 最大事实数
    - fact_confidence_threshold: 事实存储的最低置信度
    - max_injection_tokens: 注入提示词的最大 token 数
    """

    enabled: bool = Field(
        default=True,
        description="Whether to enable memory mechanism",
    )
    storage_path: str = Field(
        default="",
        description=(
            "Path to store memory data. "
            "If empty, defaults to per-user memory at `{base_dir}/users/{user_id}/memory.json`. "
            "Absolute paths are used as-is and opt out of per-user isolation "
            "(all users share the same file). "
            "Relative paths are resolved against `Paths.base_dir` "
            "(not the backend working directory). "
            "Note: if you previously set this to `.deer-flow/memory.json`, "
            "the file will now be resolved as `{base_dir}/.deer-flow/memory.json`; "
            "migrate existing data or use an absolute path to preserve the old location."
        ),
    )
    storage_class: str = Field(
        default="deerflow.agents.memory.storage.FileMemoryStorage",
        description="The class path for memory storage provider",
    )
    debounce_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Seconds to wait before processing queued updates (debounce)",
    )
    model_name: str | None = Field(
        default=None,
        description="Model name to use for memory updates (None = use default model)",
    )
    max_facts: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Maximum number of facts to store",
    )
    fact_confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum confidence threshold for storing facts",
    )
    injection_enabled: bool = Field(
        default=True,
        description="Whether to inject memory into system prompt",
    )
    max_injection_tokens: int = Field(
        default=2000,
        ge=100,
        le=8000,
        description="Maximum tokens to use for memory injection",
    )
    token_counting: Literal["tiktoken", "char"] = Field(
        default="tiktoken",
        description=(
            "Token counting strategy for memory-injection budgeting. "
            "'tiktoken' is accurate but the encoding's BPE data may be "
            "downloaded from a public network endpoint on first use, which "
            "can block for a long time in network-restricted environments "
            "(see issue #3402/#3429). 'char' uses a network-free "
            "CJK-aware character-based estimate and never touches tiktoken."
        ),
    )


# 全局单例 — 由 AppConfig._apply_singleton_configs() 更新
_memory_config: MemoryConfig = MemoryConfig()


def get_memory_config() -> MemoryConfig:
    """获取当前记忆配置（全局单例）。"""
    return _memory_config


def set_memory_config(config: MemoryConfig) -> None:
    """设置记忆配置。"""
    global _memory_config
    _memory_config = config


def load_memory_config_from_dict(config_dict: dict) -> None:
    """从字典加载记忆配置（由 AppConfig 初始化时调用）。"""
    global _memory_config
    _memory_config = MemoryConfig(**config_dict)
