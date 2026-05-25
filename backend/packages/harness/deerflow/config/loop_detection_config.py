"""循环检测配置 — 重复工具调用检测与中断。

当 Agent 陷入重复调用同一组工具的循环时，循环检测系统会：
1. 发出警告（warn_threshold），提示 Agent 改变策略
2. 强制停止（hard_limit），中断循环并要求 Agent 给出最终文本回答

### 两层检测机制

#### 模式匹配（pattern-based）
检测连续 N 次相同的工具调用集合。
例如 Agent 连续 3 次调用 [bash("ls"), bash("cat file")] 就会触发警告。

#### 频率检测（frequency-based）
检测单个工具被调用的总次数。
适用于长时间运行中某个工具被过度使用的情况（如 RNA-seq 管道中的 bash）。

### Per-tool 覆盖
不同工具的使用模式差异很大。通过 tool_freq_overrides 可以为特定工具设置不同的阈值：
例如 bash 用于批量操作时可能需要更高的频率限制。

### 验证约束
hard_limit >= warn_threshold（必须先警告再停止）
"""

from pydantic import BaseModel, Field, model_validator


class ToolFreqOverride(BaseModel):
    """按工具的频率阈值覆盖。

    可以高于或低于全局默认值。常见用法是为高频工具（如 bash）
    在批量工作流中提高阈值，而不削弱其他工具的保护。
    """

    warn: int = Field(ge=1)
    hard_limit: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate(self) -> "ToolFreqOverride":
        """确保 hard_limit >= warn，否则停止发生在警告之前（不合理）。"""
        if self.hard_limit < self.warn:
            raise ValueError("hard_limit must be >= warn")
        return self


class LoopDetectionConfig(BaseModel):
    """循环检测全局配置。

    ### 模式匹配
    - warn_threshold: 相同工具调用集合重复多少次后警告
    - hard_limit: 重复多少次后强制停止
    - window_size: 追踪的最近工具调用集合数量
    - max_tracked_threads: 内存中最多追踪的线程数

    ### 频率检测
    - tool_freq_warn: 单个工具调用多少次后警告
    - tool_freq_hard_limit: 单个工具调用多少次后停止
    - tool_freq_overrides: 按工具名的频率阈值覆盖
    """

    enabled: bool = Field(
        default=True,
        description="Whether to enable repetitive tool-call loop detection",
    )
    warn_threshold: int = Field(
        default=3,
        ge=1,
        description="Number of identical tool-call sets before injecting a warning",
    )
    hard_limit: int = Field(
        default=5,
        ge=1,
        description="Number of identical tool-call sets before forcing a stop",
    )
    window_size: int = Field(
        default=20,
        ge=1,
        description="Number of recent tool-call sets to track per thread",
    )
    max_tracked_threads: int = Field(
        default=100,
        ge=1,
        description="Maximum number of thread histories to keep in memory",
    )
    tool_freq_warn: int = Field(
        default=30,
        ge=1,
        description="Number of calls to the same tool type before injecting a frequency warning",
    )
    tool_freq_hard_limit: int = Field(
        default=50,
        ge=1,
        description="Number of calls to the same tool type before forcing a stop",
    )
    tool_freq_overrides: dict[str, ToolFreqOverride] = Field(
        default_factory=dict,
        description=("Per-tool overrides for tool_freq_warn / tool_freq_hard_limit, keyed by tool name. Values can be higher or lower than the global defaults. Commonly used to raise thresholds for high-frequency tools like bash."),
    )

    @model_validator(mode="after")
    def validate_thresholds(self) -> "LoopDetectionConfig":
        """确保 hard stop 不会在 warning 之前发生。"""
        if self.hard_limit < self.warn_threshold:
            raise ValueError("hard_limit must be greater than or equal to warn_threshold")
        if self.tool_freq_hard_limit < self.tool_freq_warn:
            raise ValueError("tool_freq_hard_limit must be greater than or equal to tool_freq_warn")
        return self
