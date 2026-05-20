"""SQLAlchemy 声明基类，提供自动 to_dict 序列化支持。

所有 DeerFlow ORM 模型均继承自本模块的 Base 类。
Base 通过 SQLAlchemy 的 inspect() 机制提供通用的 to_dict() 方法，
使各模型无需单独编写序列化逻辑，减少重复代码。

注意：LangGraph 检查点（checkpointer）的表不受此 Base 管理，
它们有独立的元数据和迁移生命周期。
"""

from __future__ import annotations

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 DeerFlow ORM 模型的基类。

    提供两个通用方法:
    - to_dict():   通过 SQLAlchemy 列自省，自动将 ORM 实例转换为字典
    - __repr__():  显示所有列的值，便于调试和日志输出

    作用：避免每个模型都手写序列化逻辑，统一管理 ORM 对象到字典的转换。
    """

    def to_dict(self, *, exclude: set[str] | None = None) -> dict:
        """将 ORM 实例转换为普通字典。

        利用 SQLAlchemy 的 inspect() 遍历所有已映射的列属性，
        自动提取列名和值，生成字典。

        这种方式的好处：
        - 不需要每个模型手动列举字段
        - 新增字段时自动包含在输出中，无需维护序列化代码

        Args:
            exclude: 可选的列名集合，这些列不会出现在输出字典中。
                     常用于排除敏感字段或内部字段。

        Returns:
            包含所有映射列的字典 {列名: 值}。
        """
        exclude = exclude or set()
        # sa_inspect(type(self)).mapper.column_attrs 获取该模型所有列属性
        # 遍历每列，跳过 exclude 集合中的列
        return {c.key: getattr(self, c.key) for c in sa_inspect(type(self)).mapper.column_attrs if c.key not in exclude}

    def __repr__(self) -> str:
        """生成可读的字符串表示，显示所有列的值。

        输出格式: ClassName(col1=val1, col2=val2, ...)
        便于在日志和调试中快速查看 ORM 对象的状态。
        """
        # 拼接所有列的名值对
        cols = ", ".join(f"{c.key}={getattr(self, c.key)!r}" for c in sa_inspect(type(self)).mapper.column_attrs)
        return f"{type(self).__name__}({cols})"
