"""
记忆存储提供者（第 2 层：存储）

本模块负责记忆数据的持久化读写，是记忆系统四层架构中的第 2 层。

数据存储模型：
  memory.json 文件中包含三大部分：
  - user: 用户画像（workContext/personalContext/topOfMind），记录用户当前状态
  - history: 历史背景（recentMonths/earlierContext/longTermBackground），按时间衰减
  - facts: 离散事实列表，支持增删改查和置信度排序

存储路径策略：
  - 按 (user_id, agent_name) 二级 key 隔离
  - 全局记忆：{base_dir}/memory.json
  - 按用户：{base_dir}/users/{user_id}/memory.json
  - 按用户+智能体：{base_dir}/users/{user_id}/agents/{agent_name}/memory.json
  - 若 config 中 storage_path 为绝对路径，则直接使用该路径（不按用户隔离）

关键设计决策：
  - 使用 JSON 文件而非数据库：零依赖部署，单文件即可工作，用户可直接编辑
  - mtime 缓存：基于文件修改时间判断缓存有效性，避免每次读磁盘
  - 原子写入：先写临时文件，再通过 os.replace() 原子重命名，防止写一半损坏
  - 抽象基类 + 工厂模式：可通过 storage_class 配置替换为 PostgreSQL 等后端

依赖关系：
  - memory_config.py：storage_path、storage_class 等配置
  - paths.py：路径解析（user_memory_file、agent_memory_file 等）
  - agents_config.py：AGENT_NAME_PATTERN，用于校验智能体名称防止路径穿越
"""

import abc
import json
import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deerflow.config.agents_config import AGENT_NAME_PATTERN
from deerflow.config.memory_config import get_memory_config
from deerflow.config.paths import get_paths

logger = logging.getLogger(__name__)


def utc_now_iso_z() -> str:
    """返回当前 UTC 时间的 ISO-8601 格式字符串（带 ``Z`` 后缀）。

    示例输出：'2026-05-20T08:00:00Z'
    使用 removesuffix("+00:00") + "Z" 的方式保持与之前 naive-UTC 输出的兼容性。
    """
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory() -> dict[str, Any]:
    """创建一个空白的记忆数据结构。

    返回包含所有预定义分区的初始 memory.json 模板：
    - user: 三个空摘要区（工作上下文、个人上下文、当前关注点）
    - history: 三个空摘要区（近期、早期、长期背景）
    - facts: 空列表
    """
    return {
        "version": "1.0",
        "lastUpdated": utc_now_iso_z(),
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


class MemoryStorage(abc.ABC):
    """记忆存储的抽象基类。

    定义 load / reload / save 三个核心接口。
    具体实现可以是文件存储（FileMemoryStorage）、数据库等。
    通过 storage_class 配置项 + get_memory_storage() 工厂函数实现可替换存储后端。
    """

    @abc.abstractmethod
    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """加载记忆数据（带缓存）。"""
        pass

    @abc.abstractmethod
    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """强制重新从存储加载记忆数据（忽略缓存）。"""
        pass

    @abc.abstractmethod
    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
        """保存记忆数据。返回 True 表示成功，False 表示失败。"""
        pass


class FileMemoryStorage(MemoryStorage):
    """基于文件的记忆存储实现。

    核心机制：
    - mtime 缓存：用文件的最后修改时间作为缓存键，load() 时先检查 mtime 是否变化，
      未变化则直接返回缓存数据，避免重复读磁盘和 JSON 解析
    - 线程安全：所有对 _memory_cache 的读写都通过 _cache_lock 保护
    - 原子写入：save() 时先写入临时文件（.tmp），再通过 os.replace() 原子重命名，
      确保不会出现写了一半的损坏文件

    缓存结构：
    - 键：(user_id, agent_name) 元组，None 表示全局
    - 值：(memory_data, file_mtime) 元组
    """

    def __init__(self):
        """初始化文件记忆存储。"""
        # 按 (user_id, agent_name) 元组建索引的缓存
        # 值为 (memory_data, file_mtime) 元组，mtime=None 表示文件不存在
        self._memory_cache: dict[tuple[str | None, str | None], tuple[dict[str, Any], float | None]] = {}
        # 保护 _memory_cache 跨并发读写的线程锁
        self._cache_lock = threading.Lock()

    def _validate_agent_name(self, agent_name: str) -> None:
        """校验智能体名称是否安全可用于文件系统路径。

        使用仓库统一的 AGENT_NAME_PATTERN 正则进行校验，防止路径穿越等安全问题。
        例如名称中不能包含 ".." 或 "/" 等字符。
        """
        if not agent_name:
            raise ValueError("Agent name must be a non-empty string.")
        if not AGENT_NAME_PATTERN.match(agent_name):
            raise ValueError(f"Invalid agent name {agent_name!r}: names must match {AGENT_NAME_PATTERN.pattern}")

    def _get_memory_file_path(self, agent_name: str | None = None, *, user_id: str | None = None) -> Path:
        """根据 user_id 和 agent_name 计算记忆文件的路径。

        路径解析优先级：
        1. user_id + agent_name → {base}/users/{user_id}/agents/{agent_name}/memory.json
        2. user_id only → config.storage_path (若为绝对路径) 或 {base}/users/{user_id}/memory.json
        3. agent_name only (无 user_id) → {base}/agents/{agent_name}/memory.json
        4. 全局 → config.storage_path (若为绝对/相对路径) 或 {base}/memory.json
        """
        if user_id is not None:
            if agent_name is not None:
                # 用户 + 智能体：按用户+智能体隔离
                self._validate_agent_name(agent_name)
                return get_paths().user_agent_memory_file(user_id, agent_name)
            # 仅用户：检查是否有自定义 storage_path
            config = get_memory_config()
            if config.storage_path and Path(config.storage_path).is_absolute():
                return Path(config.storage_path)
            # 使用默认的用户级路径
            return get_paths().user_memory_file(user_id)
        # 无 user_id（旧版兼容）
        if agent_name is not None:
            self._validate_agent_name(agent_name)
            return get_paths().agent_memory_file(agent_name)
        config = get_memory_config()
        if config.storage_path:
            p = Path(config.storage_path)
            return p if p.is_absolute() else get_paths().base_dir / p
        return get_paths().memory_file

    def _load_memory_from_file(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """从磁盘读取并解析记忆 JSON 文件。

        若文件不存在或解析失败，返回空白记忆结构（不会抛出异常）。
        """
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)

        if not file_path.exists():
            return create_empty_memory()

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load memory file: %s", e)
            return create_empty_memory()

    @staticmethod
    def _cache_key(agent_name: str | None = None, *, user_id: str | None = None) -> tuple[str | None, str | None]:
        """生成缓存键：以 (user_id, agent_name) 元组作为唯一标识。"""
        return (user_id, agent_name)

    def load(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """加载记忆数据（带 mtime 缓存）。

        流程：
        1. 获取文件的 mtime（最后修改时间）
        2. 检查缓存中是否有该键的数据，且 mtime 是否一致
        3. mtime 一一致 → 直接返回缓存数据
        4. mtime 不一致或无缓存 → 从文件重新读取并更新缓存

        这种设计意味着外部手动编辑 JSON 文件也能被检测到（mtime 变化），
        比固定 TTL 缓存更灵活。
        """
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            current_mtime = None

        with self._cache_lock:
            cached = self._memory_cache.get(cache_key)
            if cached is not None and cached[1] == current_mtime:
                # mtime 未变化，直接返回缓存
                return cached[0]

        # mtime 变化或无缓存，从文件重新加载
        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)

        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, current_mtime)

        return memory_data

    def reload(self, agent_name: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """强制重新加载记忆数据（忽略缓存）。

        与 load() 的区别：不检查缓存，直接从文件读取后更新缓存。
        用于外部调用方明确需要最新数据的场景（如 API 手动刷新）。
        """
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        memory_data = self._load_memory_from_file(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            mtime = None

        with self._cache_lock:
            self._memory_cache[cache_key] = (memory_data, mtime)
        return memory_data

    def save(self, memory_data: dict[str, Any], agent_name: str | None = None, *, user_id: str | None = None) -> bool:
        """保存记忆数据到文件并更新缓存。

        原子写入流程：
        1. 浅拷贝 memory_data 并设置 lastUpdated 时间戳（避免修改调用方的原始 dict）
        2. 写入到随机命名的临时文件（uuid.tmp）
        3. 通过 os.replace() 原子重命名为目标文件（POSIX 原子操作）
        4. 更新内存缓存

        返回 True 表示保存成功，False 表示写入失败。
        """
        file_path = self._get_memory_file_path(agent_name, user_id=user_id)
        cache_key = self._cache_key(agent_name, user_id=user_id)

        try:
            # 确保父目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # 浅拷贝后设置 lastUpdated，避免修改调用方的原始 dict，
            # 也防止缓存引用在文件写入成功前被静默更新
            memory_data = {**memory_data, "lastUpdated": utc_now_iso_z()}

            # 原子写入：先写临时文件，再重命名
            temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(memory_data, f, indent=2, ensure_ascii=False)

            # os.replace() 在 POSIX 上是原子操作，在 Windows 上也能可靠替换
            temp_path.replace(file_path)

            # 保存成功后更新缓存的 mtime
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = None

            with self._cache_lock:
                self._memory_cache[cache_key] = (memory_data, mtime)
            logger.info("Memory saved to %s", file_path)
            return True
        except OSError as e:
            logger.error("Failed to save memory file: %s", e)
            return False


# ---- 全局单例 ----

_storage_instance: MemoryStorage | None = None
_storage_lock = threading.Lock()


def get_memory_storage() -> MemoryStorage:
    """获取已配置的记忆存储实例（全局单例，线程安全）。

    通过 memory_config.py 中的 storage_class 配置项动态加载存储实现：
    - 默认值为 "deerflow.agents.memory.storage.FileMemoryStorage"
    - 通过 importlib 反射加载模块和类
    - 加载失败时自动 fallback 到 FileMemoryStorage

    使用双重检查锁定（double-checked locking）保证线程安全的懒初始化。
    """
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    with _storage_lock:
        if _storage_instance is not None:
            return _storage_instance

        config = get_memory_config()
        storage_class_path = config.storage_class

        try:
            # 反射加载：从 "module.path.ClassName" 格式解析出模块和类
            module_path, class_name = storage_class_path.rsplit(".", 1)
            import importlib

            module = importlib.import_module(module_path)
            storage_class = getattr(module, class_name)

            # 校验加载到的是 MemoryStorage 的子类
            if not isinstance(storage_class, type):
                raise TypeError(f"Configured memory storage '{storage_class_path}' is not a class: {storage_class!r}")
            if not issubclass(storage_class, MemoryStorage):
                raise TypeError(f"Configured memory storage '{storage_class_path}' is not a subclass of MemoryStorage")

            _storage_instance = storage_class()
        except Exception as e:
            # 加载失败，回退到默认的文件存储
            logger.error(
                "Failed to load memory storage %s, falling back to FileMemoryStorage: %s",
                storage_class_path,
                e,
            )
            _storage_instance = FileMemoryStorage()

    return _storage_instance
