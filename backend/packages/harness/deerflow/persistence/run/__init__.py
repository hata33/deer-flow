"""运行（Run）元数据持久化子包 —— ORM 模型和 SQL 仓库。

本子包负责管理运行（Run）的元数据，包括运行状态、模型信息、
Token 用量统计等。与 RunEventStore（事件日志）不同，这里存储的是
运行的汇总信息。

导出:
  - RunRow:        运行元数据表的 ORM 模型类
  - RunRepository: 基于 SQLAlchemy 的运行数据仓库，实现 RunStore 接口
"""

from deerflow.persistence.run.model import RunRow
from deerflow.persistence.run.sql import RunRepository

__all__ = ["RunRepository", "RunRow"]
