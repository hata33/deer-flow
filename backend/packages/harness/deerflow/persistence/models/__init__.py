"""ORM 模型注册入口点。

导入此模块可确保所有 ORM 模型都注册到 ``Base.metadata`` 中，
使 Alembic 自动生成检测到所有表，也让 init_engine 的自动建表功能正常工作。

实际的 ORM 类分布在各实体子包中:
  - ``deerflow.persistence.thread_meta``   —— 线程元数据
  - ``deerflow.persistence.run``           —— 运行元数据
  - ``deerflow.persistence.feedback``      —— 用户反馈
  - ``deerflow.persistence.user``          —— 用户信息

``RunEventRow`` 保留在 ``deerflow.persistence.models.run_event`` 中，
因为其存储实现位于 ``deerflow.runtime.events.store.db``，
没有对应的实体目录。

为什么需要这个模块:
  SQLAlchemy 的 Base.metadata 只知道被导入过的模型。
  这个模块集中导入所有模型，确保 Base.metadata 包含完整的表定义。
"""

from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

__all__ = ["FeedbackRow", "RunEventRow", "RunRow", "ThreadMetaRow", "UserRow"]
