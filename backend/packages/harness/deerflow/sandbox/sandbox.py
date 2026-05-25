"""Sandbox 抽象基类 —— 定义所有沙箱实现必须遵循的标准接口。

本模块定义了 :class:`Sandbox` 抽象基类，它是沙箱子系统的核心接口契约。
所有沙箱后端（本地文件系统、Docker 容器、远程执行环境等）都必须实现此基类
定义的全部抽象方法。

Sandbox 接口为 Agent 提供了以下核心能力：

- **命令执行**：在沙箱隔离环境中执行 bash/shell 命令
- **文件操作**：读取、写入、更新文件内容
- **文件下载**：下载文件的原始二进制数据
- **目录遍历**：列出目录内容（支持递归深度控制）
- **文件搜索**：glob 模式匹配和 grep 内容搜索

虚拟路径约定
~~~~~~~~~~~~~
所有 Sandbox 方法的路径参数均使用**虚拟路径**（container path），而非宿主机
的真实路径。具体的路径映射由子类实现（如 :class:`LocalSandbox` 使用
``PathMapping`` 进行 container_path ↔ local_path 的双向映射）。

常见的虚拟路径约定：

- ``/mnt/user-data/workspace/`` — Agent 的工作空间（可读写）
- ``/mnt/user-data/uploads/`` — 用户上传文件（可读写）
- ``/mnt/user-data/outputs/`` — 输出文件（可读写）
- ``/mnt/skills/`` — 技能文件（只读）
- ``/mnt/acp-workspace`` — ACP 工作空间（可读写）

安全性
~~~~~~~
Sandbox 接口在文档层面要求所有实现：

- 防止路径遍历攻击（如 ``../../etc/passwd``）
- 强制执行只读挂载限制（向只读路径写入应抛出 ``PermissionError``）
- 在输出中隐藏宿主机真实路径，只暴露虚拟路径
"""

from abc import ABC, abstractmethod

from deerflow.sandbox.search import GrepMatch


class Sandbox(ABC):
    """沙箱环境的抽象基类。

    定义了 Agent 在隔离环境中操作文件和执行命令的标准接口。
    每个沙箱实例都有一个唯一的标识符 ``id``，用于在 Provider 中索引和管理。

    子类必须实现所有标记为 ``@abstractmethod`` 的方法。

    Attributes:
        _id: 沙箱的唯一标识符（如 ``"local"`` 或 ``"local:thread_abc123"``）。
    """

    _id: str

    def __init__(self, id: str):
        """初始化沙箱实例。

        Args:
            id: 沙箱的唯一标识符，由 SandboxProvider 在 acquire 时分配。
        """
        self._id = id

    @property
    def id(self) -> str:
        """获取沙箱的唯一标识符。"""
        return self._id

    @abstractmethod
    def execute_command(self, command: str) -> str:
        """在沙箱隔离环境中执行 bash 命令。

        命令在沙箱的隔离上下文中执行（容器、虚拟机或受限的本地环境）。
        返回命令的标准输出和/或错误输出。

        实现注意事项：
        - 命令中的虚拟路径应被自动解析为宿主机路径
        - 输出中的宿主机路径应被自动替换回虚拟路径
        - 命令执行应有超时限制

        Args:
            command: 要执行的命令字符串。

        Returns:
            命令的标准输出或错误输出文本。
        """
        pass

    @abstractmethod
    def read_file(self, path: str) -> str:
        """读取文件内容。

        以 UTF-8 编码读取指定路径的文件内容。

        Args:
            path: 文件的绝对虚拟路径。

        Returns:
            文件的文本内容。

        Raises:
            OSError: 如果文件不存在或无法读取。
        """
        pass

    @abstractmethod
    def download_file(self, path: str) -> bytes:
        """下载文件的原始二进制数据。

        与 ``read_file`` 不同，本方法返回原始字节，适用于二进制文件
        （图片、压缩包等）。

        安全限制：
        - 必须检测并拒绝路径遍历攻击
        - 必须确保路径在允许的虚拟路径前缀下
        - 应检查文件大小限制（防止下载过大文件）

        Args:
            path: 文件的绝对虚拟路径。

        Returns:
            文件的原始字节数据。

        Raises:
            PermissionError: 检测到路径遍历或路径不在允许的前缀下。
            OSError: 文件不存在或无法读取。所有实现（本地和远程）
                必须抛出 ``OSError``，以便调用方使用统一的异常类型处理。
        """
        pass

    @abstractmethod
    def list_dir(self, path: str, max_depth=2) -> list[str]:
        """列出目录内容。

        递归遍历目录树，返回文件和子目录的路径列表。目录路径以 ``/`` 结尾
        作为标识。

        Args:
            path: 目录的绝对虚拟路径。
            max_depth: 最大递归深度（默认 2）。
                1 = 仅直接子项，2 = 子项 + 孙项，以此类推。

        Returns:
            目录内容的绝对路径列表（虚拟路径），目录以 ``/`` 结尾。
        """
        pass

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写入文件内容。

        将文本内容写入指定路径的文件。如果文件不存在则创建，如果文件已存在
        则根据 ``append`` 参数决定是追加还是覆盖。

        安全限制：
        - 必须检查目标路径是否在只读挂载下，如果是则拒绝写入
        - 如果父目录不存在，应自动创建

        Args:
            path: 文件的绝对虚拟路径。
            content: 要写入的文本内容。
            append: 是否追加模式。如果为 False，文件将被创建或覆盖。
        """
        pass

    @abstractmethod
    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """使用 glob 模式搜索文件路径。

        在指定目录下递归搜索匹配 glob 模式的文件和目录。

        Args:
            path: 搜索根目录的绝对虚拟路径。
            pattern: glob 模式字符串（如 ``"*.py"``、``"**/*.json"``）。
            include_dirs: 是否在结果中包含目录。
            max_results: 最大返回结果数。

        Returns:
            ``(matches, truncated)`` 元组：
            - matches: 匹配的虚拟路径列表。
            - truncated: 结果是否因达到上限被截断。
        """
        pass

    @abstractmethod
    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        """在文本文件中搜索匹配的行。

        在指定目录下递归搜索文本文件，返回包含匹配内容的行。

        Args:
            path: 搜索根目录的绝对虚拟路径。
            pattern: 搜索模式（正则表达式或字面量）。
            glob: 可选的 glob 文件名过滤模式。
            literal: 是否将 pattern 视为字面量文本。
            case_sensitive: 是否区分大小写。
            max_results: 最大返回匹配数。

        Returns:
            ``(matches, truncated)`` 元组：
            - matches: :class:`GrepMatch` 对象列表。
            - truncated: 结果是否因达到上限被截断。
        """
        pass

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None:
        """以二进制模式更新文件。

        将原始字节数据写入指定路径。适用于二进制文件（图片、压缩包等），
        或者需要精确控制文件内容的场景。

        安全限制：
        - 必须检查目标路径是否在只读挂载下

        Args:
            path: 文件的绝对虚拟路径。
            content: 要写入的原始字节数据。
        """
        pass
