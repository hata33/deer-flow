"""沙箱异常层次结构。

提供结构化的错误信息，包含 sandbox_id、command、exit_code、path、operation 等上下文。
异常层次：SandboxError → SandboxNotFoundError / SandboxRuntimeError / SandboxCommandError / SandboxFileError
"""


class SandboxError(Exception):
    """沙箱异常基类，携带结构化错误详情。"""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
        return self.message


class SandboxNotFoundError(SandboxError):
    """沙箱未找到或不可用。"""

    def __init__(self, message: str = "Sandbox not found", sandbox_id: str | None = None):
        details = {"sandbox_id": sandbox_id} if sandbox_id else None
        super().__init__(message, details)
        self.sandbox_id = sandbox_id


class SandboxRuntimeError(SandboxError):
    """沙箱运行时不可用或配置错误。"""

    pass


class SandboxCommandError(SandboxError):
    """沙箱中命令执行失败，携带命令内容和退出码。"""

    def __init__(self, message: str, command: str | None = None, exit_code: int | None = None):
        details = {}
        if command:
            details["command"] = command[:100] + "..." if len(command) > 100 else command
        if exit_code is not None:
            details["exit_code"] = exit_code
        super().__init__(message, details)
        self.command = command
        self.exit_code = exit_code


class SandboxFileError(SandboxError):
    """沙箱中文件操作失败，携带路径和操作类型。"""

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
    """沙箱中文件操作权限不足。"""

    pass


class SandboxFileNotFoundError(SandboxFileError):
    """沙箱中文件或目录未找到。"""

    pass
