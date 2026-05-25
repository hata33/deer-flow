"""本地文件系统沙箱实现 —— 直接在宿主机文件系统上模拟沙箱环境。

本子包提供了 :class:`LocalSandboxProvider` 和 :class:`LocalSandbox`，它们是
:class:`~deerflow.sandbox.sandbox.Sandbox` 和
:class:`~deerflow.sandbox.sandbox_provider.SandboxProvider` 的本地文件系统实现。

与 Docker 容器沙箱（AioSandboxProvider）不同，本地沙箱直接在宿主机上操作文件
和执行命令，不提供进程级别的隔离。它通过**路径映射**和**输出屏蔽**来模拟
沙箱的隔离效果。

核心组件
~~~~~~~~
- :class:`LocalSandbox` — 沙箱实例，实现路径映射、命令执行、文件操作等
- :class:`LocalSandboxProvider` — Provider 实现，管理每线程的沙箱实例，带 LRU 缓存
- :func:`list_dir` — 目录递归遍历工具

路径映射系统
~~~~~~~~~~~~
LocalSandbox 的核心是**路径映射**（PathMapping），它建立了虚拟路径与宿主机
真实路径之间的双向映射：

- **正向解析**（Forward Resolve）：虚拟路径 → 宿主机路径
  - 用于命令执行和文件操作前，将 Agent 提供的虚拟路径转换为宿主机路径
  - 例如 ``/mnt/user-data/workspace/app.py`` → ``/home/user/threads/t1/user-data/workspace/app.py``

- **反向解析**（Reverse Resolve）：宿主机路径 → 虚拟路径
  - 用于命令输出和文件列表中，将宿主机真实路径替换为虚拟路径
  - 确保 Agent 看不到宿主机文件系统的真实结构

- **输出屏蔽**（Output Masking）：自动将输出中的宿主机路径替换为虚拟路径
  - 命令执行的 stdout/stderr
  - 目录列表结果
  - Agent 写入的文件内容（read_file 时反向解析）

目录结构
~~~~~~~~~
::

    local/
    ├── __init__.py                 # 本文件
    ├── local_sandbox.py            # LocalSandbox 实现（路径映射、命令执行）
    ├── local_sandbox_provider.py   # LocalSandboxProvider（LRU 缓存、每线程沙箱）
    └── list_dir.py                 # 目录递归遍历工具函数
"""

from .local_sandbox_provider import LocalSandboxProvider

__all__ = ["LocalSandboxProvider"]
