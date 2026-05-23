"""SkillStorage 单例 + 基于反射的工厂函数。

与 ``deerflow/sandbox/sandbox_provider.py`` 采用相同的模式：
- 进程级单例缓存，避免重复创建存储实例。
- 基于配置的反射加载，通过 ``use`` 字段指定实现类。
- 支持按需创建新实例（用于测试和自定义路径）。

单例策略
  当调用方不传入 ``skills_path`` 或 ``app_config`` 时，返回进程级单例。
  单例在首次调用时通过 ``get_app_config()`` 创建，之后复用。
  如果全局配置发生变化（``_default_skill_storage_config`` 不匹配），
  自动重建单例。

按需创建
  当传入 ``skills_path`` 或 ``app_config`` 时，总是创建新实例：
  - ``skills_path``: 覆盖宿主机路径，类仍由配置解析。
    如果没有 ``app_config``，使用默认 ``SkillsConfig()`` 以避免读取 config.yaml。
  - ``app_config``: 使用请求级配置（如 Gateway ``Depends(get_config)``），
    不污染进程级单例。

测试支持
  ``reset_skill_storage()`` 清除单例缓存，用于测试隔离和热重载场景。
"""

from __future__ import annotations

from deerflow.skills.storage.local_skill_storage import LocalSkillStorage
from deerflow.skills.storage.skill_storage import SkillStorage

# 进程级单例及其对应的配置标识
_default_skill_storage: SkillStorage | None = None
_default_skill_storage_config: object | None = None  # 创建单例时使用的 AppConfig 标识


def get_or_new_skill_storage(**kwargs) -> SkillStorage:
    """返回 ``SkillStorage`` 实例 —— 可以是新实例或进程单例。

    **创建新实例**（不缓存）的条件:
    - 传入 ``skills_path`` —— 用作 ``host_path`` 覆盖（类仍通过配置解析）。
    - 传入 ``app_config`` —— 从 ``app_config.skills`` 构建存储，
      使请求级配置（如 Gateway ``Depends(get_config)``）生效
      而不污染进程级单例。

    **返回单例**的条件（首次调用创建，之后复用）:
    - 既不传 ``skills_path`` 也不传 ``app_config`` ——
      使用 ``get_app_config()`` 解析当前活动配置。

    Returns:
        ``SkillStorage`` 实例。
    """
    global _default_skill_storage, _default_skill_storage_config

    from deerflow.config import get_app_config
    from deerflow.config.skills_config import SkillsConfig

    def _make_storage(skills_config: SkillsConfig, *, host_path: str | None = None, **kwargs) -> SkillStorage:
        """通过反射加载 ``skills_config.use`` 指定的类并实例化。"""
        from deerflow.reflection import resolve_class

        cls = resolve_class(skills_config.use, SkillStorage)
        return cls(
            host_path=host_path if host_path is not None else str(
                skills_config.get_skills_path()),
            container_path=skills_config.container_path,
            **kwargs,
        )

    skills_path = kwargs.pop("skills_path", None)
    app_config = kwargs.pop("app_config", None)

    if skills_path is not None:
        if app_config is not None:
            return _make_storage(app_config.skills, host_path=str(skills_path), **kwargs)
        # 无 app_config：使用默认 SkillsConfig，避免读取 config.yaml
        from deerflow.config.skills_config import SkillsConfig

        return _make_storage(SkillsConfig(), host_path=str(skills_path), **kwargs)

    if app_config is not None:
        # 有 app_config：创建新实例，不污染单例
        return _make_storage(app_config.skills, **kwargs)

    # 如果单例已通过测试注入且没有 config 标识（_default_skill_storage_config 为 None），
    # 则完全跳过 get_app_config()，避免要求磁盘上存在 config.yaml。
    if _default_skill_storage is not None and _default_skill_storage_config is None:
        return _default_skill_storage

    app_config_now = get_app_config()
    # 单例不存在或配置已变更 → 重建
    if _default_skill_storage is None or _default_skill_storage_config is not app_config_now:
        _default_skill_storage = _make_storage(app_config_now.skills, **kwargs)
        _default_skill_storage_config = app_config_now
    return _default_skill_storage


def reset_skill_storage() -> None:
    """清除缓存的单例（用于测试和热重载场景）。"""
    global _default_skill_storage, _default_skill_storage_config
    _default_skill_storage = None
    _default_skill_storage_config = None


__all__ = [
    "LocalSkillStorage",
    "SkillStorage",
    "get_or_new_skill_storage",
    "reset_skill_storage",
]
