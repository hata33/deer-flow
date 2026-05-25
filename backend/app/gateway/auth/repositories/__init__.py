"""用户数据仓库子包 — 存储后端抽象与实现。

本子包实现了仓库模式（Repository Pattern），将用户数据访问逻辑
从认证业务逻辑中解耦。

模块结构：
  - base.py   — UserRepository 抽象接口，定义所有必须实现的方法
  - sqlite.py — SQLite/SQLAlchemy 实现，使用共享的异步会话工厂

设计要点：
  - 抽象接口确保存储后端可替换（SQLite → PostgreSQL → 等）
  - 所有方法都是异步的，与 FastAPI 的异步架构一致
  - UserNotFoundError 作为 LookupError 子类，兼容已有的异常处理
"""
