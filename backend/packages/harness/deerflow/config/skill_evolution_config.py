"""技能演化配置 — Agent 自主创建和修改技能。

技能演化允许 Agent 在运行时创建新技能或修改已有技能。
这在需要 Agent 具备自我学习能力的场景中很有用。

### 安全考虑
- 技能修改只影响 skills/custom/ 目录下的自定义技能
- 公共技能（skills/public/）不可被 Agent 修改
- 可选的审核模型（moderation_model_name）用于安全审查 Agent 创建的技能

### 默认关闭
此功能默认关闭，需要显式启用。

本配置是 AppConfig 的直接字段（不是全局单例）。
"""

from pydantic import BaseModel, Field


class SkillEvolutionConfig(BaseModel):
    """Agent 自主技能演化配置。

    - enabled: 是否允许 Agent 创建和修改 skills/custom 下的技能
    - moderation_model_name: 技能安全审核使用的模型（None = 主聊天模型）
    """

    enabled: bool = Field(
        default=False,
        description="Whether the agent can create and modify skills under skills/custom.",
    )
    moderation_model_name: str | None = Field(
        default=None,
        description="Optional model name for skill security moderation. Defaults to the primary chat model.",
    )
