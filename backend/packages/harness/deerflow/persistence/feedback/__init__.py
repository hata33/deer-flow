"""反馈（Feedback）持久化子包 —— ORM 模型和 SQL 仓库。

本子包负责管理用户对运行（Run）的反馈数据，包括点赞/点踩和评论。

导出:
  - FeedbackRow:        反馈表的 ORM 模型类
  - FeedbackRepository: 基于 SQLAlchemy 的反馈数据仓库
"""

# 导入 ORM 模型和数据仓库类，统一通过子包入口暴露
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.feedback.sql import FeedbackRepository

__all__ = ["FeedbackRepository", "FeedbackRow"]
