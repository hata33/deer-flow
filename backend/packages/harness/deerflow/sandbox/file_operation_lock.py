"""文件级并发操作锁 —— 防止同一文件的并发写入或字符串替换导致数据损坏。

在多线程环境下，多个工具调用可能同时尝试对同一个文件执行写操作（write_file）
或字符串替换操作（str_replace）。如果这些操作不被串行化，可能导致文件内容
交错、数据丢失或损坏。

本模块通过**每个文件一把锁**的机制来解决这个问题：

1. 全局维护一个 ``sandbox_id → path → Lock`` 的映射表。
2. 任何需要对文件进行写操作的代码在操作前获取对应的锁。
3. 使用 :class:`weakref.WeakValueDictionary` 存储锁对象，确保在不再有线程
   引用某把锁时，锁对象会被自动垃圾回收，避免长期运行进程中出现内存泄漏。

设计要点
~~~~~~~~
- **锁粒度**：锁的粒度为 ``(sandbox_id, path)`` 元组，即同一个沙箱内的
  同一个文件共享一把锁，不同文件互不影响。
- **弱引用**：使用 ``WeakValueDictionary`` 确保无引用的锁能被 GC 回收。
- **全局保护**：``_FILE_OPERATION_LOCKS_GUARD`` 是一把全局互斥锁，保护
  ``_FILE_OPERATION_LOCKS`` 字典本身的创建操作不被并发访问。

使用方式::

    from deerflow.sandbox.file_operation_lock import get_file_operation_lock

    lock = get_file_operation_lock(sandbox, "/mnt/user-data/workspace/file.py")
    with lock:
        # 在此区域内，其他线程无法对该文件执行并发写操作
        sandbox.write_file("/mnt/user-data/workspace/file.py", content)
"""

import threading
import weakref

from deerflow.sandbox.sandbox import Sandbox

# 使用 WeakValueDictionary 存储文件操作锁，防止长期运行进程中出现内存泄漏。
# 当某把锁不再被任何线程引用时，会自动从字典中移除并被垃圾回收。
_LockKey = tuple[str, str]

# 全局锁映射表：(sandbox_id, path) → threading.Lock
_FILE_OPERATION_LOCKS: weakref.WeakValueDictionary[_LockKey, threading.Lock] = weakref.WeakValueDictionary()

# 保护 _FILE_OPERATION_LOCKS 字典访问的全局互斥锁。
# 由于多个线程可能同时发现某个 key 不存在并同时尝试创建锁，
# 必须用这把锁来串行化 get-or-create 操作。
_FILE_OPERATION_LOCKS_GUARD = threading.Lock()


def get_file_operation_lock_key(sandbox: Sandbox, path: str) -> tuple[str, str]:
    """生成文件操作锁的键。

    键为 ``(sandbox_id, path)`` 元组，确保同一沙箱内的同一文件共享同一把锁，
    而不同沙箱或不同文件使用不同的锁。

    Args:
        sandbox: 沙箱实例，通过 ``sandbox.id`` 获取沙箱标识。
        path: 文件的绝对路径（虚拟路径）。

    Returns:
        ``(sandbox_id, path)`` 元组作为锁的键。
    """
    sandbox_id = getattr(sandbox, "id", None)
    if not sandbox_id:
        # 如果沙箱没有 id 属性，使用对象内存地址作为后备标识
        sandbox_id = f"instance:{id(sandbox)}"
    return sandbox_id, path


def get_file_operation_lock(sandbox: Sandbox, path: str) -> threading.Lock:
    """获取指定沙箱中指定文件的操作锁。

    如果该 ``(sandbox_id, path)`` 组合尚无对应的锁，则创建一把新锁并注册到
    全局映射表中。后续对该文件的并发请求将复用同一把锁。

    该方法是**线程安全**的：使用 ``_FILE_OPERATION_LOCKS_GUARD`` 全局互斥锁
    保护 get-or-create 操作，确保并发线程不会创建重复的锁。

    Args:
        sandbox: 沙箱实例。
        path: 文件的绝对路径（虚拟路径）。

    Returns:
        与 ``(sandbox.id, path)`` 关联的 :class:`threading.Lock` 实例。
    """
    lock_key = get_file_operation_lock_key(sandbox, path)
    with _FILE_OPERATION_LOCKS_GUARD:
        lock = _FILE_OPERATION_LOCKS.get(lock_key)
        if lock is None:
            # 首次访问该文件，创建新锁并存入弱引用字典
            lock = threading.Lock()
            _FILE_OPERATION_LOCKS[lock_key] = lock
        return lock
