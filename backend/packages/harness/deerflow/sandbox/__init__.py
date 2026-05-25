"""沙箱（Sandbox）子系统 —— 为 Agent 提供隔离的文件与命令执行环境。

本模块是 DeerFlow 沙箱子系统的顶层入口，对外暴露的核心接口包括：

- :class:`Sandbox` — 所有沙箱实现的**抽象基类**，定义了文件读写、目录遍历、
  glob/grep 搜索、命令执行等标准操作。不同的后端（本地文件系统、远程容器、
  Docker 等）通过子类来实现具体的沙箱语义。
- :class:`SandboxProvider` — 沙箱的**工厂与生命周期管理器**，负责创建（acquire）、
  获取（get）、释放（release）沙箱实例。Provider 遵循单例模式，整个进程共享
  一个 Provider 实例。
- :func:`get_sandbox_provider` — 获取全局 Provider 单例的便捷函数。

虚拟路径体系
~~~~~~~~~~~~~
沙箱使用一套**虚拟路径（Virtual Path）**体系，将容器视角的路径与宿主机的
真实路径隔离开来。核心映射规则如下：

- ``/mnt/user-data/`` → 每个线程专属的宿主目录（如
  ``{base}/users/{user_id}/threads/{thread_id}/user-data/``）
- ``/mnt/acp-workspace`` → 每个线程的 ACP 工作空间目录
- ``/mnt/skills`` → 技能目录（只读挂载）

Agent 只能看到虚拟路径，宿主机真实路径在输出中会被自动屏蔽（reverse resolve），
以确保安全性与可移植性。

目录结构
~~~~~~~~~
::

    sandbox/
    ├── __init__.py              # 本文件，模块入口
    ├── sandbox.py               # Sandbox 抽象基类
    ├── sandbox_provider.py      # SandboxProvider 抽象基类与全局单例管理
    ├── exceptions.py            # 沙箱专用异常层次结构
    ├── security.py              # 安全门控（本地沙箱 bash 限制等）
    ├── file_operation_lock.py   # 文件级并发操作锁
    ├── search.py                # glob/grep 文件搜索（含忽略模式）
    ├── middleware.py            # Agent 中间件，管理沙箱生命周期
    └── local/                   # 本地文件系统沙箱实现
        ├── local_sandbox.py           # LocalSandbox（路径映射、命令执行）
        ├── local_sandbox_provider.py  # LocalSandboxProvider（LRU 缓存）
        └── list_dir.py               # 目录递归遍历工具
"""

from .sandbox import Sandbox
from .sandbox_provider import SandboxProvider, get_sandbox_provider

__all__ = [
    "Sandbox",
    "SandboxProvider",
    "get_sandbox_provider",
]
