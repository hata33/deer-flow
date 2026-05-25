"""沙箱专用异常层次结构 —— 为沙箱操作提供结构化的错误信息。

本模块定义了沙箱子系统中所有异常类的层次结构，遵循以下设计原则：

1. **统一基类**：所有沙箱异常均继承自 :class:`SandboxError`，调用方可以只捕获
   这一顶级异常，也可以针对特定场景捕获子类。
2. **结构化详情**：每个异常除了人类可读的 ``message`` 外，还携带 ``details``
   字典，包含与错误场景相关的机器可读字段（如路径、命令、退出码等），
   便于日志记录和上层逻辑处理。
3. **异常分类**：
   - :class:`SandboxError` — 基类，包含 message + details
   - :class:`SandboxNotFoundError` — 沙箱实例找不到（如 ID 无效或已释放）
   - :class:`SandboxRuntimeError` — 沙箱运行时不可用或配置错误
   - :class:`SandboxCommandError` — 命令执行失败（含命令内容和退出码）
   - :class:`SandboxFileError` — 文件操作失败（含路径和操作类型）
   - :class:`SandboxPermissionError` — 权限不足（如只读挂载写入）
   - :class:`SandboxFileNotFoundError` — 文件或目录不存在

异常继承关系::

    SandboxError
    ├── SandboxNotFoundError
    ├── SandboxRuntimeError
    ├── SandboxCommandError
    └── SandboxFileError
        ├── SandboxPermissionError
        └── SandboxFileNotFoundError
"""


class SandboxError(Exception):
    """所有沙箱相关异常的基类。

    携带人类可读的 ``message`` 和结构化的 ``details`` 字典。
    ``__str__`` 方法会将 details 格式化为 ``key=value`` 形式附加到消息末尾，
    便于日志输出和调试。

    Attributes:
        message: 错误描述文本。
        details: 与错误相关的结构化键值对，如 ``{"path": "/mnt/data/f.txt"}``。
    """

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        # 如果未提供 details，使用空字典以避免 None 检查
        self.details = details or {}

    def __str__(self) -> str:
        """格式化输出，包含 details 中的键值对。"""
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
        return self.message


class SandboxNotFoundError(SandboxError):
    """沙箱实例未找到或不可用时抛出。

    典型场景：
    - 使用了无效或已释放的 sandbox_id 调用 ``provider.get()``
    - 沙箱后端（如 Docker 容器）意外终止

    Attributes:
        sandbox_id: 找不到的沙箱标识符（可选）。
    """

    def __init__(self, message: str = "Sandbox not found", sandbox_id: str | None = None):
        details = {"sandbox_id": sandbox_id} if sandbox_id else None
        super().__init__(message, details)
        self.sandbox_id = sandbox_id


class SandboxRuntimeError(SandboxError):
    """沙箱运行时不可用或配置错误时抛出。

    典型场景：
    - Docker 守护进程未运行（AioSandboxProvider）
    - 沙箱配置项缺失或格式错误
    """
    pass


class SandboxCommandError(SandboxError):
    """沙箱内命令执行失败时抛出。

    携带失败的命令文本（截断至 100 字符）和进程退出码，
    便于诊断命令执行问题。

    Attributes:
        command: 执行失败的命令（完整文本保留，details 中截断显示）。
        exit_code: 进程退出码（可选）。
    """

    def __init__(self, message: str, command: str | None = None, exit_code: int | None = None):
        details = {}
        if command:
            # 命令文本在 details 中截断至 100 字符，避免日志过长
            details["command"] = command[:100] + "..." if len(command) > 100 else command
        if exit_code is not None:
            details["exit_code"] = exit_code
        super().__init__(message, details)
        self.command = command
        self.exit_code = exit_code


class SandboxFileError(SandboxError):
    """沙箱内文件操作失败时抛出。

    携带目标文件路径和操作类型（如 "read"、"write"、"download"），
    便于定位文件相关问题。

    Attributes:
        path: 操作涉及的文件路径（可选）。
        operation: 操作类型描述（可选）。
    """

    def __init__(self, message: str, path: str | None = None, operation: str | None = None):
        details = {}
        if path:
            details["path"] = path
        if operation:
            details["operation"] = operation
        super().__init__(message, details)
        self.path = path
        self.operation = operation


class SandboxPermissionError(SandboxFileError):
    """沙箱文件操作权限不足时抛出。

    典型场景：
    - 尝试向只读挂载路径写入文件（如 ``/mnt/skills``）
    - 路径遍历攻击被检测到（如 ``../../etc/passwd``）
    - download_file 访问了虚拟路径前缀之外的路径
    """
    pass


class SandboxFileNotFoundError(SandboxFileError):
    """沙箱内文件或目录不存在时抛出。

    典型场景：
    - 读取不存在的文件
    - 列出已删除的目录
    """
    pass
