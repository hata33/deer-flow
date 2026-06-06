"""
AIO Sandbox 实例 — 与 agent-infra/sandbox Docker 容器交互的核心类

本模块实现了 DeerFlow 沙箱接口（Sandbox），通过 HTTP API 与运行中的
AIO 沙箱容器进行通信。AIO 沙箱基于 OpenHands 的 agent-infra/sandbox
项目，提供了 Shell 命令执行、文件读写、文件搜索等功能。

核心设计:
    - 线程安全: 通过 threading.Lock 序列化所有对容器的 HTTP 请求，防止
      并发操作导致容器内单会话状态损坏（参见 issue #1433）
    - 会话恢复: 当检测到 ErrorObservation 错误签名时，自动在新的会话中
      重试命令，提高容错性
    - 路径安全: 对文件下载操作进行路径遍历检查，确保只能访问虚拟路径前缀
      下的文件，防止安全漏洞
    - 流式下载: 大文件下载采用分块流式处理，并设置 100MB 上限防止内存溢出

依赖:
    - agent_sandbox: OpenHands 的 sandbox Python 客户端库
    - deerflow.sandbox.sandbox: DeerFlow 沙箱抽象基类
    - deerflow.sandbox.search: 文件搜索工具（GrepMatch 等）
"""

import base64
import errno
import logging
import shlex
import threading
import uuid

from agent_sandbox import Sandbox as AioSandboxClient

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import GrepMatch, path_matches, should_ignore_path, truncate_line

logger = logging.getLogger(__name__)

# 文件下载的最大允许大小（100 MB），防止内存溢出
_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024  # 100 MB

# ErrorObservation 错误签名：当容器会话因并发冲突损坏时返回的错误特征字符串
_ERROR_OBSERVATION_SIGNATURE = "'ErrorObservation' object has no attribute 'exit_code'"


class AioSandbox(Sandbox):
    """基于 agent-infra/sandbox Docker 容器的沙箱实现。

    通过 HTTP API 连接到运行中的 AIO 沙箱容器。使用线程锁序列化 Shell 命令
    以防止并发请求导致容器的单持久会话状态损坏（参见 #1433）。

    该类继承自 DeerFlow 的 Sandbox 抽象基类，实现了所有沙箱操作接口：
    命令执行、文件读写、目录列表、文件搜索（glob/grep）等。

    线程安全说明:
        所有涉及 HTTP 请求的操作都通过 self._lock 进行序列化。这确保了
        在多线程环境下，容器内的单会话不会被并发请求破坏。
    """

    def __init__(self, id: str, base_url: str, home_dir: str | None = None):
        """初始化 AIO 沙箱实例。

        Args:
            id: 沙箱实例的唯一标识符，通常由 Provider 生成的确定性 ID。
            base_url: 沙箱 API 的 URL 地址（例如 http://localhost:8080），
                      容器内的 sandbox 服务在此地址上监听。
            home_dir: 沙箱容器内的用户主目录路径。如果为 None，将在首次
                      访问时从沙箱上下文中自动获取。
        """
        super().__init__(id)
        self._base_url = base_url
        self._client = AioSandboxClient(base_url=base_url, timeout=600)
        self._home_dir = home_dir
        self._lock = threading.Lock()
        self._closed = False

    @property
    def base_url(self) -> str:
        """获取沙箱 API 的基础 URL 地址。

        Returns:
            沙箱 API 的 URL 字符串。
        """
        return self._base_url

    def close(self) -> None:
        """Best-effort close of the host-side HTTP client owned by this sandbox.

        The agent_sandbox SDK is Fern-generated and exposes no ``close()`` /
        ``__exit__``, so we reach the socket-owning ``httpx.Client`` explicitly
        through its attribute chain::

            Sandbox._client_wrapper        -> SyncClientWrapper
                .httpx_client              -> Fern HttpClient (a wrapper, NOT httpx.Client)
                    .httpx_client          -> httpx.Client     <- the real socket owner

        Closing it releases pooled sockets so long-running provider lifecycles
        do not accumulate unreclaimed host-side resources (#2872).

        Resolution is most-specific-first with graceful degradation: if a future
        SDK adds a top-level ``Sandbox.close()`` it is picked up automatically
        without changing this code. Idempotent, thread-safe, and non-fatal:
        failures during teardown are logged and swallowed so provider/backend
        cleanup is never blocked.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            # Drop the reference under the lock for use-after-close safety: any
            # later command on this instance fails loudly instead of reusing a
            # half-closed client.
            self._client = None

        if client is None:
            return

        # Walk from the real httpx.Client up to the top-level client, picking the
        # first object that actually exposes close().
        wrapper = getattr(client, "_client_wrapper", None)
        fern_http = getattr(wrapper, "httpx_client", None)
        real_httpx = getattr(fern_http, "httpx_client", None)
        target = next(
            (c for c in (real_httpx, fern_http, client) if c is not None and hasattr(c, "close")),
            None,
        )
        if target is None:
            logger.debug("AioSandbox %s: no closable client found, nothing to release", self.id)
            return

        try:
            target.close()
        except Exception as e:
            logger.warning(f"Error closing AioSandbox client for {self.id}: {e}")

    @property
    def home_dir(self) -> str:
        """获取沙箱容器内的用户主目录路径。

        首次访问时如果未指定 home_dir，会向沙箱发送 get_context 请求
        来获取容器内的实际主目录路径，后续访问将使用缓存值。

        Returns:
            容器内的用户主目录绝对路径。
        """
        if self._home_dir is None:
            context = self._client.sandbox.get_context()
            self._home_dir = context.home_dir
        return self._home_dir

    # 默认无变更超时时间（秒）。与客户端级别超时保持一致，确保长时间运行
    # 但无输出的命令不会被沙箱内置的 120 秒默认值过早终止。
    _DEFAULT_NO_CHANGE_TIMEOUT = 600

    def execute_command(self, command: str) -> str:
        """在沙箱中执行 Shell 命令。

        使用线程锁序列化并发请求。AIO 沙箱容器维护一个单持久 Shell 会话，
        并发调用 exec_command 会导致会话损坏（返回 ErrorObservation 而非
        真实输出）。即使加锁后仍可能检测到损坏（例如多个进程共享同一沙箱），
        此时会使用新的会话 ID 重试命令。

        Args:
            command: 要执行的 Shell 命令字符串。

        Returns:
            命令的标准输出内容。如果命令无输出，返回 "(no output)"。
            如果执行失败，返回 "Error: <错误信息>" 格式的字符串。
        """
        with self._lock:
            try:
                result = self._client.shell.exec_command(command=command, no_change_timeout=self._DEFAULT_NO_CHANGE_TIMEOUT)
                output = result.data.output if result.data else ""

                # 检测到会话损坏的错误签名，使用新的会话 ID 重试
                if output and _ERROR_OBSERVATION_SIGNATURE in output:
                    logger.warning("ErrorObservation detected in sandbox output, retrying with a fresh session")
                    fresh_id = str(uuid.uuid4())
                    result = self._client.shell.exec_command(command=command, id=fresh_id, no_change_timeout=self._DEFAULT_NO_CHANGE_TIMEOUT)
                    output = result.data.output if result.data else ""

                return output if output else "(no output)"
            except Exception as e:
                logger.error(f"Failed to execute command in sandbox: {e}")
                return f"Error: {e}"

    def read_file(self, path: str) -> str:
        """读取沙箱中的文件内容。

        通过沙箱 HTTP API 读取指定路径的文件内容。

        Args:
            path: 要读取的文件绝对路径（容器内的路径）。

        Returns:
            文件的文本内容。如果读取失败，返回 "Error: <错误信息>" 格式的字符串。
        """
        try:
            result = self._client.file.read_file(file=path)
            return result.data.content if result.data else ""
        except Exception as e:
            logger.error(f"Failed to read file in sandbox: {e}")
            return f"Error: {e}"

    def download_file(self, path: str) -> bytes:
        """从沙箱下载文件的二进制内容。

        采用分块流式下载，并限制最大下载大小为 100MB。下载前会进行
        路径安全检查，拒绝路径遍历攻击和虚拟路径前缀外的文件访问。

        Args:
            path: 要下载的文件绝对路径（容器内的路径）。

        Returns:
            文件的二进制内容。

        Raises:
            PermissionError: 如果路径包含 ".." 遍历段或路径位于
                VIRTUAL_PATH_PREFIX 之外。
            OSError: 如果文件无法从沙箱中获取（包括超过大小限制，
                错误码为 EFBIG）。
        """
        # 在发送到容器 API 之前拒绝路径遍历攻击。
        # LocalSandbox 通过 _resolve_path 隐式获得此保护；
        # 这里路径被原样转发，因此必须显式检查。
        normalised = path.replace("\\", "/")
        for segment in normalised.split("/"):
            if segment == "..":
                logger.error(f"Refused download due to path traversal: {path}")
                raise PermissionError(f"Access denied: path traversal detected in '{path}'")

        # 确保文件路径在允许的虚拟路径前缀下
        stripped_path = normalised.lstrip("/")
        allowed_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
        if stripped_path != allowed_prefix and not stripped_path.startswith(f"{allowed_prefix}/"):
            logger.error("Refused download outside allowed directory: path=%s, allowed_prefix=%s", path, VIRTUAL_PATH_PREFIX)
            raise PermissionError(f"Access denied: path must be under '{VIRTUAL_PATH_PREFIX}': '{path}'")

        with self._lock:
            try:
                chunks: list[bytes] = []
                total = 0
                for chunk in self._client.file.download_file(path=path):
                    total += len(chunk)
                    # 超过最大下载大小时立即中断并抛出错误
                    if total > _MAX_DOWNLOAD_SIZE:
                        raise OSError(
                            errno.EFBIG,
                            f"File exceeds maximum download size of {_MAX_DOWNLOAD_SIZE} bytes",
                            path,
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
            except OSError:
                raise
            except Exception as e:
                logger.error(f"Failed to download file in sandbox: {e}")
                raise OSError(f"Failed to download file '{path}' from sandbox: {e}") from e

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """列出沙箱中指定目录的内容。

        通过在容器内执行 find 命令来获取目录列表，同时获取文件和子目录。

        Args:
            path: 要列出的目录绝对路径（容器内的路径）。
            max_depth: 目录遍历的最大深度，默认为 2 层。结果限制为 500 条。

        Returns:
            目录内容的路径列表。每个元素是容器内的绝对路径。
            如果操作失败，返回空列表。
        """
        with self._lock:
            try:
                result = self._client.shell.exec_command(command=f"find {shlex.quote(path)} -maxdepth {max_depth} -type f -o -type d 2>/dev/null | head -500", no_change_timeout=self._DEFAULT_NO_CHANGE_TIMEOUT)
                output = result.data.output if result.data else ""
                if output:
                    return [line.strip() for line in output.strip().split("\n") if line.strip()]
                return []
            except Exception as e:
                logger.error(f"Failed to list directory in sandbox: {e}")
                return []

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """向沙箱中的文件写入内容。

        支持覆盖写入和追加写入两种模式。追加模式会先读取现有内容，
        将新内容拼接到末尾后整体写入。

        Args:
            path: 要写入的文件绝对路径（容器内的路径）。
            content: 要写入的文本内容。
            append: 是否以追加模式写入。默认为 False（覆盖模式）。

        Raises:
            Exception: 如果写入操作失败。
        """
        with self._lock:
            try:
                if append:
                    existing = self.read_file(path)
                    # 如果读取成功（非错误响应），将现有内容与新内容拼接
                    if not existing.startswith("Error:"):
                        content = existing + content
                self._client.file.write_file(file=path, content=content)
            except Exception as e:
                logger.error(f"Failed to write file in sandbox: {e}")
                raise

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """在沙箱中按 glob 模式搜索文件。

        支持两种搜索策略：
        - 仅文件模式（include_dirs=False）：使用沙箱的 find_files API
        - 包含目录模式（include_dirs=True）：使用 list_path API 递归列出
          所有条目后在客户端进行模式匹配

        Args:
            path: 搜索的根目录路径。
            pattern: Glob 匹配模式（例如 "*.py", "**/*.txt"）。
            include_dirs: 是否在结果中包含目录。默认为 False。
            max_results: 最大返回结果数。默认为 200。

        Returns:
            元组 (匹配的文件路径列表, 是否因达到上限而被截断)。
        """
        if not include_dirs:
            result = self._client.file.find_files(path=path, glob=pattern)
            files = result.data.files if result.data and result.data.files else []
            filtered = [file_path for file_path in files if not should_ignore_path(file_path)]
            truncated = len(filtered) > max_results
            return filtered[:max_results], truncated

        # 包含目录时需要递归列出路径后进行客户端模式匹配
        result = self._client.file.list_path(path=path, recursive=True, show_hidden=False)
        entries = result.data.files if result.data and result.data.files else []
        matches: list[str] = []
        root_path = path.rstrip("/") or "/"
        root_prefix = root_path if root_path == "/" else f"{root_path}/"
        for entry in entries:
            if entry.path != root_path and not entry.path.startswith(root_prefix):
                continue
            if should_ignore_path(entry.path):
                continue
            # 计算相对路径用于模式匹配
            rel_path = entry.path[len(root_path) :].lstrip("/")
            if path_matches(pattern, rel_path):
                matches.append(entry.path)
                if len(matches) >= max_results:
                    return matches, True
        return matches, False

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
        """在沙箱文件中搜索匹配的文本行。

        使用正则表达式在沙箱文件中搜索匹配的内容。支持通过 glob 模式
        限定搜索范围，以及字面量搜索和大小写敏感选项。

        搜索策略：
        - 如果指定了 glob 模式，先通过 find_files 获取候选文件列表
        - 否则通过 list_path 递归列出所有非目录文件作为候选

        Args:
            path: 搜索的根目录路径。
            pattern: 搜索模式（正则表达式或字面量文本）。
            glob: 可选的 glob 模式，用于限定搜索的文件范围。
            literal: 是否将 pattern 视为字面量文本（自动转义特殊字符）。
            case_sensitive: 是否区分大小写。默认为 False（不区分）。
            max_results: 最大返回匹配结果数。默认为 100。

        Returns:
            元组 (GrepMatch 匹配结果列表, 是否因达到上限而被截断)。
        """
        import re as _re

        regex_source = _re.escape(pattern) if literal else pattern
        # 在本地验证正则表达式，使无效正则抛出 re.error（被 grep_tool
        # 的 except re.error 处理器捕获），而不是产生通用的远程 API 错误。
        _re.compile(regex_source, 0 if case_sensitive else _re.IGNORECASE)
        regex = regex_source if case_sensitive else f"(?i){regex_source}"

        if glob is not None:
            # 使用 glob 模式筛选候选文件
            find_result = self._client.file.find_files(path=path, glob=glob)
            candidate_paths = find_result.data.files if find_result.data and find_result.data.files else []
        else:
            # 无 glob 时列出所有非目录文件
            list_result = self._client.file.list_path(path=path, recursive=True, show_hidden=False)
            entries = list_result.data.files if list_result.data and list_result.data.files else []
            candidate_paths = [entry.path for entry in entries if not entry.is_directory]

        matches: list[GrepMatch] = []
        truncated = False

        for file_path in candidate_paths:
            if should_ignore_path(file_path):
                continue

            search_result = self._client.file.search_in_file(file=file_path, regex=regex)
            data = search_result.data
            if data is None:
                continue

            line_numbers = data.line_numbers or []
            matched_lines = data.matches or []
            for line_number, line in zip(line_numbers, matched_lines):
                matches.append(
                    GrepMatch(
                        path=file_path,
                        line_number=line_number if isinstance(line_number, int) else 0,
                        line=truncate_line(line),
                    )
                )
                if len(matches) >= max_results:
                    truncated = True
                    return matches, truncated

        return matches, truncated

    def update_file(self, path: str, content: bytes) -> None:
        """使用二进制内容更新沙箱中的文件。

        将二进制内容进行 Base64 编码后通过沙箱 API 写入文件。

        Args:
            path: 要更新的文件绝对路径（容器内的路径）。
            content: 要写入的二进制内容。

        Raises:
            Exception: 如果写入操作失败。
        """
        with self._lock:
            try:
                base64_content = base64.b64encode(content).decode("utf-8")
                self._client.file.write_file(file=path, content=base64_content, encoding="base64")
            except Exception as e:
                logger.error(f"Failed to update file in sandbox: {e}")
                raise
