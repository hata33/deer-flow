"""用户（User）存储子包。

包含 users 表的 ORM 模型定义。

为什么没有 Repository:
  具体的仓库实现（SQLiteUserRepository）位于应用层
  ``app.gateway.auth.repositories.sqlite`` 中，
  因为它需要在 ORM 行和 auth 模块的 Pydantic ``User`` 类之间转换。

  将 ORM 模型放在 harness 持久化包中的原因:
    - 确保 Base.metadata.create_all() 能发现并创建 users 表
    - 与其他表（threads_meta, runs, run_events, feedback）共享同一个引擎
    - 保持 harness 包对应用代码的独立性（harness 不导入 app 代码）
"""

from deerflow.persistence.user.model import UserRow

__all__ = ["UserRow"]
