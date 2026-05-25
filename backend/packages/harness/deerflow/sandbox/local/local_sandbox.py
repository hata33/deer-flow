"""本地文件系统沙箱 —— 通过路径映射实现虚拟路径隔离。

本模块实现了 :class:`LocalSandbox`，它是 :class:`~deerflow.sandbox.sandbox.Sandbox`
的本地文件系统实现。LocalSandbox 不使用容器或虚拟机隔离，而是通过**路径映射**
（PathMapping）在宿主机文件系统上模拟沙箱的虚拟路径空间。

核心概念
~~~~~~~~

路径映射（PathMapping）
^^^^^^^^^^^^^^^^^^^^^^^
每条 PathMapping 定义了一条从**容器路径**（Agent 看到的虚拟路径）到**本地路径**
（宿主机真实路径）的映射，并可标记为只读：

::

    PathMapping(
        container_path="/mnt/skills",      # 虚拟路径
        local_path="/opt/deerflow/skills", # 宿主机真实路径
        read_only=True                     # 只读挂载
    )

正向解析（Forward Resolve）
^^^^^^^^^^^^^^^^^^^^^^^^^^
将 Agent 提供的虚拟路径转换为宿主机真实路径：

- ``_find_path_mapping(path)`` — 查找匹配的 PathMapping（最长前缀优先）
- ``_resolve_path(path)`` — 返回解析后的宿主机路径字符串
- ``_resolve_path_with_mapping(path)`` — 返回宿主机路径 + 匹配的 PathMapping

反向解析（Reverse Resolve）
^^^^^^^^^^^^^^^^^^^^^^^^^^
将宿主机真实路径转换回虚拟路径，用于输出屏蔽：

- ``_reverse_resolve_path(path)`` — 单个路径的反向解析
- ``_reverse_resolve_paths_in_output(output)`` — 字符串中的批量路径替换

输出屏蔽（Output Masking）
^^^^^^^^^^^^^^^^^^^^^^^^^
所有输出给 Agent 的内容中，宿主机真实路径都会被自动替换为虚拟路径：

- 命令执行的 stdout/stderr
- 目录列表结果
- Agent 写入的文件内容（read_file 时反向解析，仅限 agent-authored 文件）

安全性
~~~~~~~
- **路径遍历防护**：``_resolve_path_with_mapping`` 检查解析后的路径是否逃逸出
  映射的本地根目录（如 ``/mnt/user-data/../../etc/passwd``）
- **只读挂载强制**：``write_file`` 和 ``update_file`` 检查目标路径是否在只读
  映射下，如果是则抛出 ``OSError(EROFS)``
- **下载路径限制**：``download_file`` 限制只能下载 ``/mnt/user-data/`` 前缀下的文件
- **符号链接安全**：grep/glob 搜索中跳过符号链接并检查路径是否在根目录内

Shell 检测
~~~~~~~~~~
自动检测可用的 shell（zsh > bash > sh > PowerShell > cmd.exe），
并针对不同 shell 类型使用正确的命令行参数。
"""

import errno
import logging
import ntpath
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.local.list_dir import list_dir
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import GrepMatch, find_glob_matches, find_grep_matches

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PathMapping:
    """从容器路径到本地路径的映射，支持可选的只读标志。

    Attributes:
        container_path: Agent 视角的虚拟路径（如 ``/mnt/skills``）。
        local_path: 宿主机上的真实路径（如 ``/opt/deerflow/skills``）。
        read_only: 是否为只读挂载。如果为 True，向此路径写入会抛出
            ``OSError(EROFS)``。默认为 False。
    """

    container_path: str
    local_path: str
    read_only: bool = False


class ResolvedPath(NamedTuple):
    """路径解析结果，包含解析后的本地路径和匹配的映射。

    Attributes:
        path: 解析后的宿主机绝对路径。
        mapping: 匹配的 PathMapping（如果没有匹配则为 None）。
    """
    path: str
    mapping: PathMapping | None


class LocalSandbox(Sandbox):
    """基于本地文件系统的沙箱实现。

    通过 PathMapping 列表实现虚拟路径到宿主机路径的双向映射。
    所有文件操作和命令执行都在宿主机上直接进行，通过路径映射和输出屏蔽
    模拟沙箱的隔离效果。

    Agent-authored 文件追踪
    ~~~~~~~~~~~~~~~~~~~~~~~
    ``_agent_written_paths`` 集合记录了通过 ``write_file`` 写入的文件路径。
    这些文件在 ``read_file`` 时会进行反向路径解析（将宿主机路径替换回虚拟路径），
    因为 Agent 写入的内容中可能包含虚拟路径引用。
    用户上传的文件和外部工具输出不做此处理，以避免意外修改。
    """

    @staticmethod
    def _shell_name(shell: str) -> str:
        """从 shell 路径或命令中提取可执行文件名（小写）。

        Args:
            shell: shell 的完整路径或命令名（如 ``/bin/bash`` 或 ``powershell``）。

        Returns:
            小写的可执行文件名（如 ``bash`` 或 ``powershell.exe``）。
        """
        return shell.replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _is_powershell(shell: str) -> bool:
        """判断是否为 PowerShell 可执行文件。"""
        return LocalSandbox._shell_name(shell) in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}

    @staticmethod
    def _is_cmd_shell(shell: str) -> bool:
        """判断是否为 cmd.exe。"""
        return LocalSandbox._shell_name(shell) in {"cmd", "cmd.exe"}

    @staticmethod
    def _is_msys_shell(shell: str) -> bool:
        """判断是否为 Git Bash / MSYS shell。

        通过检查 shell 路径中是否包含 ``/git/``、``/mingw`` 或 ``/msys`` 来判断。
        """
        normalized = shell.replace("\\", "/").lower()
        shell_name = LocalSandbox._shell_name(shell)
        return shell_name in {"sh.exe", "bash.exe"} and any(part in normalized for part in ("/git/", "/mingw", "/msys"))

    @staticmethod
    def _find_first_available_shell(candidates: tuple[str, ...]) -> str | None:
        """从候选列表中找到第一个可用的 shell。

        对绝对路径检查文件是否存在且可执行，对命令名使用 ``which`` 查找。

        Args:
            candidates: 按优先级排列的 shell 路径/命令元组。

        Returns:
            第一个可用的 shell 路径，如果都不可用则返回 None。
        """
        for shell in candidates:
            if os.path.isabs(shell):
                # 绝对路径：检查文件存在且可执行
                if os.path.isfile(shell) and os.access(shell, os.X_OK):
                    return shell
                continue

            # 命令名：通过 PATH 查找
            shell_from_path = shutil.which(shell)
            if shell_from_path is not None:
                return shell_from_path

        return None

    def __init__(self, id: str, path_mappings: list[PathMapping] | None = None):
        """初始化本地沙箱实例。

        Args:
            id: 沙箱标识符（如 ``"local"`` 或 ``"local:thread_abc123"``）。
            path_mappings: 路径映射列表。每个映射定义了从容器路径到本地路径的
                对应关系。技能目录默认为只读。
        """
        super().__init__(id)
        self.path_mappings = path_mappings or []
        # 追踪通过 write_file 写入的文件路径，read_file 时仅对这些文件
        # 进行反向路径解析。用户上传文件和外部工具输出不做此处理。
        self._agent_written_paths: set[str] = set()

    def _is_read_only_path(self, resolved_path: str) -> bool:
        """检查已解析的路径是否位于只读挂载下。

        当多个映射匹配（嵌套挂载）时，选择最具体的映射（即 local_path
        为最长前缀的那个），与 ``_resolve_path`` 处理容器路径的方式一致。

        Args:
            resolved_path: 已解析的宿主机绝对路径。

        Returns:
            如果路径位于只读挂载下返回 True。
        """
        resolved = str(Path(resolved_path).resolve())

        best_mapping: PathMapping | None = None
        best_prefix_len = -1

        for mapping in self.path_mappings:
            local_resolved = str(Path(mapping.local_path).resolve())
            if resolved == local_resolved or resolved.startswith(local_resolved + os.sep):
                prefix_len = len(local_resolved)
                # 选择最长前缀的映射（最具体的挂载点）
                if prefix_len > best_prefix_len:
                    best_prefix_len = prefix_len
                    best_mapping = mapping

        if best_mapping is None:
            # 不在任何映射下，不标记为只读
            return False

        return best_mapping.read_only

    def _find_path_mapping(self, path: str) -> tuple[PathMapping, str] | None:
        """查找路径匹配的 PathMapping 及其相对路径部分。

        按 container_path 长度从长到短排序（最长前缀优先），
        找到第一个匹配的映射后返回映射和路径的相对部分。

        Args:
            path: 待匹配的路径字符串（虚拟路径）。

        Returns:
            ``(mapping, relative_path)`` 元组，如果无匹配则返回 None。
        """
        path_str = str(path)

        for mapping in sorted(self.path_mappings, key=lambda m: len(m.container_path.rstrip("/") or "/"), reverse=True):
            container_path = mapping.container_path.rstrip("/") or "/"
            if container_path == "/":
                # 根路径映射：匹配所有以 / 开头的路径
                if path_str.startswith("/"):
                    return mapping, path_str.lstrip("/")
                continue

            if path_str == container_path or path_str.startswith(container_path + "/"):
                # 精确匹配或前缀匹配：提取相对路径部分
                relative = path_str[len(container_path) :].lstrip("/")
                return mapping, relative

        return None

    def _resolve_path_with_mapping(self, path: str) -> ResolvedPath:
        """将容器路径解析为宿主机本地路径，同时返回匹配的映射。

        正向解析的核心方法。通过 ``_find_path_mapping`` 查找匹配的 PathMapping，
        然后将相对路径部分拼接到映射的本地路径上。

        安全检查：解析后的路径必须位于映射的本地根目录内，否则抛出
        ``PermissionError``（防止路径遍历攻击）。

        Args:
            path: 可能是容器路径的路径字符串。

        Returns:
            :class:`ResolvedPath` 包含解析后的本地路径和匹配的映射。

        Raises:
            PermissionError: 如果解析后的路径逃逸出映射的本地根目录。
        """
        path_str = str(path)

        mapping_match = self._find_path_mapping(path_str)
        if mapping_match is None:
            # 无匹配的映射，返回原始路径
            return ResolvedPath(path_str, None)

        mapping, relative = mapping_match
        local_root = Path(mapping.local_path).resolve()
        resolved_path = (local_root / relative).resolve() if relative else local_root

        # 路径遍历防护：确保解析后的路径仍在映射的本地根目录内
        try:
            resolved_path.relative_to(local_root)
        except ValueError as exc:
            raise PermissionError(errno.EACCES, "Access denied: path escapes mounted directory", path_str) from exc

        return ResolvedPath(str(resolved_path), mapping)

    def _resolve_path(self, path: str) -> str:
        """将容器路径解析为宿主机本地路径（仅返回路径字符串）。

        :meth:`_resolve_path_with_mapping` 的简化版本，不返回映射信息。

        Args:
            path: 容器路径。

        Returns:
            解析后的宿主机路径字符串。
        """
        return self._resolve_path_with_mapping(path).path

    def _is_resolved_path_read_only(self, resolved: ResolvedPath) -> bool:
        """检查已解析的路径是否为只读。

        同时检查：
        1. 路径映射的 read_only 标志
        2. 路径是否位于某个只读映射的本地路径下

        Args:
            resolved: 已解析的路径结果。

        Returns:
            如果路径为只读返回 True。
        """
        return bool(resolved.mapping and resolved.mapping.read_only) or self._is_read_only_path(resolved.path)

    def _reverse_resolve_path(self, path: str) -> str:
        """将宿主机本地路径反向解析为容器路径。

        反向解析的核心方法。按 local_path 长度从长到短排序（最长前缀优先），
        找到匹配的映射后，将本地路径前缀替换为容器路径。

        Args:
            path: 宿主机本地路径。

        Returns:
            对应的容器路径。如果无匹配的映射，返回原始路径。
        """
        normalized_path = path.replace("\\", "/")
        path_str = str(Path(normalized_path).resolve())

        # 按 local_path 长度从长到短排序，优先匹配最具体的映射
        for mapping in sorted(self.path_mappings, key=lambda m: len(m.local_path), reverse=True):
            local_path_resolved = str(Path(mapping.local_path).resolve())
            if path_str == local_path_resolved or path_str.startswith(local_path_resolved + "/"):
                # 将本地路径前缀替换为容器路径
                relative = path_str[len(local_path_resolved) :].lstrip("/")
                resolved = f"{mapping.container_path}/{relative}" if relative else mapping.container_path
                return resolved

        # 无匹配的映射，返回原始路径
        return path_str

    def _reverse_resolve_paths_in_output(self, output: str) -> str:
        """将输出字符串中的宿主机本地路径批量替换为容器路径。

        输出屏蔽的核心方法。扫描输出中的所有绝对路径，将匹配映射的本地路径
        替换为对应的容器路径，确保 Agent 看不到宿主机文件系统的真实结构。

        Args:
            output: 可能包含本地路径的输出字符串。

        Returns:
            将本地路径替换为容器路径后的输出字符串。
        """
        import re

        # 按 local_path 长度从长到短排序，确保正确的最长前缀匹配
        sorted_mappings = sorted(self.path_mappings, key=lambda m: len(m.local_path), reverse=True)

        if not sorted_mappings:
            return output

        # 逐个映射扫描并替换匹配的路径
        result = output
        for mapping in sorted_mappings:
            # 转义本地路径中的正则特殊字符
            escaped_local = re.escape(str(Path(mapping.local_path).resolve()))
            # 匹配本地路径后跟可选的路径组件（支持 / 和 \ 分隔符）
            pattern = re.compile(escaped_local + r"(?:[/\\][^\s\"';&|<>()]*)?")

            def replace_match(match: re.Match) -> str:
                matched_path = match.group(0)
                return self._reverse_resolve_path(matched_path)

            result = pattern.sub(replace_match, result)

        return result

    def _resolve_paths_in_command(self, command: str) -> str:
        """将命令字符串中的容器路径解析为宿主机本地路径。

        在命令执行前调用，确保命令中的虚拟路径被替换为宿主机可以识别的
        真实路径。

        使用 shell 感知的边界字符（空格、引号、分号等）来识别路径边界，
        防止部分匹配（如 ``/mnt/skills`` 不应匹配 ``/mnt/skills-extra``）。

        Args:
            command: 可能包含容器路径的命令字符串。

        Returns:
            容器路径被解析为本地路径后的命令字符串。
        """
        import re

        # 按 container_path 长度从长到短排序，确保正确的最长前缀匹配
        sorted_mappings = sorted(self.path_mappings, key=lambda m: len(m.container_path), reverse=True)

        if not sorted_mappings:
            return command

        # 构建匹配所有容器路径的正则表达式。
        # 前瞻断言 (?=/|$|...) 确保只在路径段边界匹配，
        # 防止 /mnt/skills 匹配 /mnt/skills-extra 中的前缀。
        patterns = [re.escape(m.container_path) + r"(?=/|$|[\s\"';&|<>()])(?:/[^\s\"';&|<>()]*)?" for m in sorted_mappings]
        pattern = re.compile("|".join(f"({p})" for p in patterns))

        def replace_match(match: re.Match) -> str:
            matched_path = match.group(0)
            return self._resolve_path(matched_path)

        return pattern.sub(replace_match, command)

    def _resolve_paths_in_content(self, content: str) -> str:
        """将文件内容中的容器路径解析为宿主机本地路径。

        与 ``_resolve_paths_in_command`` 不同，本方法将内容视为纯文本，
        不使用 shell 感知的边界字符。解析后的路径统一使用正斜杠，
        避免 Windows 反斜杠在源码文件中产生无效的转义序列。

        在 ``write_file`` 中调用，确保 Agent 写入的文件内容中的虚拟路径
        被替换为宿主机真实路径。

        Args:
            content: 可能包含容器路径的文件内容。

        Returns:
            容器路径被解析为本地路径后的内容（正斜杠格式）。
        """
        import re

        sorted_mappings = sorted(self.path_mappings, key=lambda m: len(m.container_path), reverse=True)
        if not sorted_mappings:
            return content

        # 使用非单词字符作为路径边界，适合纯文本内容
        patterns = [re.escape(m.container_path) + r"(?=/|$|[^\w./-])(?:/[^\s\"';&|<>()]*)?" for m in sorted_mappings]
        pattern = re.compile("|".join(f"({p})" for p in patterns))

        def replace_match(match: re.Match) -> str:
            matched_path = match.group(0)
            resolved = self._resolve_path(matched_path)
            # 统一为正斜杠，防止 Windows 反斜杠在源码文件中产生无效转义序列
            # 例如 C:\Users\.. 中的 \U 会被 Python 解析为 Unicode 转义
            return resolved.replace("\\", "/")

        return pattern.sub(replace_match, content)

    @staticmethod
    def _get_shell() -> str:
        """检测可用的 shell 可执行文件。

        优先级：zsh > bash > sh > PowerShell > cmd.exe。
        在 Unix 系统上只查找 Unix shell；在 Windows 上还会尝试 PowerShell
        和 cmd.exe 作为后备。

        Returns:
            可用的 shell 可执行文件路径。

        Raises:
            RuntimeError: 如果没有找到任何可用的 shell。
        """
        shell = LocalSandbox._find_first_available_shell(("/bin/zsh", "/bin/bash", "/bin/sh", "sh"))
        if shell is not None:
            return shell

        # Windows 后备方案
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            shell = LocalSandbox._find_first_available_shell(
                (
                    "pwsh",
                    "pwsh.exe",
                    "powershell",
                    "powershell.exe",
                    ntpath.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                    "cmd.exe",
                )
            )
            if shell is not None:
                return shell

            raise RuntimeError("No suitable shell executable found. Tried /bin/zsh, /bin/bash, /bin/sh, `sh` on PATH, then PowerShell and cmd.exe fallbacks for Windows.")

        raise RuntimeError("No suitable shell executable found. Tried /bin/zsh, /bin/bash, /bin/sh, and `sh` on PATH.")

    def execute_command(self, command: str) -> str:
        """在本地 shell 中执行命令。

        执行流程：
        1. 将命令中的容器路径解析为本地路径
        2. 检测可用的 shell
        3. 根据 shell 类型构造正确的命令行参数
        4. 执行命令（超时 600 秒）
        5. 将输出中的本地路径反向解析为容器路径

        对于 MSYS/Git Bash shell，会设置环境变量禁用自动路径转换，
        避免路径被 MSYS 层修改。

        Args:
            command: 要执行的命令字符串（可能包含容器路径）。

        Returns:
            命令输出（stdout + stderr），本地路径已替换为容器路径。
        """
        # 执行前：将命令中的容器路径解析为本地路径
        resolved_command = self._resolve_paths_in_command(command)
        shell = self._get_shell()

        if os.name == "nt":
            # Windows 平台：根据 shell 类型使用不同的参数格式
            env = None
            if self._is_powershell(shell):
                args = [shell, "-NoProfile", "-Command", resolved_command]
            elif self._is_cmd_shell(shell):
                args = [shell, "/c", resolved_command]
            else:
                # Unix-like shell on Windows (Git Bash, MSYS)
                args = [shell, "-c", resolved_command]
                if self._is_msys_shell(shell):
                    # 禁用 MSYS 的自动路径转换，避免路径被修改
                    env = {
                        **os.environ,
                        "MSYS_NO_PATHCONV": "1",
                        "MSYS2_ARG_CONV_EXCL": "*",
                    }

            result = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
        else:
            # Unix 平台：使用 -c 参数执行命令
            args = [shell, "-c", resolved_command]
            result = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
        # 合并 stdout 和 stderr
        output = result.stdout
        if result.stderr:
            output += f"\nStd Error:\n{result.stderr}" if output else result.stderr
        if result.returncode != 0:
            output += f"\nExit Code: {result.returncode}"

        final_output = output if output else "(no output)"
        # 执行后：将输出中的本地路径反向解析为容器路径
        return self._reverse_resolve_paths_in_output(final_output)

    def list_dir(self, path: str, max_depth=2) -> list[str]:
        """列出目录内容，路径自动映射。

        解析虚拟路径为本地路径，调用 list_dir 获取结果，
        然后将结果中的本地路径反向解析为虚拟路径。

        Args:
            path: 目录的虚拟路径。
            max_depth: 最大递归深度。

        Returns:
            虚拟路径列表，目录以 ``/`` 后缀标识。
        """
        resolved_path = self._resolve_path(path)
        entries = list_dir(resolved_path, max_depth)
        # 反向解析：本地路径 → 虚拟路径，并保留 list_dir 的 "/" 目录标识
        result: list[str] = []
        for entry in entries:
            is_dir = entry.endswith(("/", "\\"))
            reversed_entry = self._reverse_resolve_path(entry.rstrip("/\\")) if is_dir else self._reverse_resolve_path(entry)
            result.append(f"{reversed_entry}/" if is_dir and not reversed_entry.endswith("/") else reversed_entry)
        return result

    def read_file(self, path: str) -> str:
        """读取文件内容。

        仅对 Agent 通过 write_file 写入的文件内容进行反向路径解析。
        用户上传的文件、外部工具输出等不做此处理，避免意外修改原始内容。

        错误处理：OSError 被重新抛出时使用原始虚拟路径，隐藏内部解析后的路径。

        Args:
            path: 文件的虚拟路径。

        Returns:
            文件的文本内容。

        Raises:
            OSError: 文件不存在或无法读取（使用虚拟路径）。
        """
        resolved_path = self._resolve_path(path)
        try:
            with open(resolved_path, encoding="utf-8") as f:
                content = f.read()
            # 仅对 Agent 通过 write_file 写入的文件进行反向路径解析。
            # 用户上传的文件、外部工具输出等不应被静默修改。
            if resolved_path in self._agent_written_paths:
                content = self._reverse_resolve_paths_in_output(content)
            return content
        except OSError as e:
            # 使用原始虚拟路径重新抛出，隐藏内部解析后的宿主机路径
            raise type(e)(e.errno, e.strerror, path) from None

    def download_file(self, path: str) -> bytes:
        """下载文件的原始二进制数据。

        安全限制：
        - 仅允许下载 ``/mnt/user-data/`` 前缀下的文件
        - 文件大小上限 100MB

        Args:
            path: 文件的虚拟路径。

        Returns:
            文件的原始字节数据。

        Raises:
            PermissionError: 路径不在允许的前缀下。
            OSError: 文件不存在、过大或无法读取（使用虚拟路径）。
        """
        normalised = path.replace("\\", "/")
        stripped_path = normalised.lstrip("/")
        allowed_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
        # 安全校验：确保路径在虚拟路径前缀下
        if stripped_path != allowed_prefix and not stripped_path.startswith(f"{allowed_prefix}/"):
            logger.error("Refused download outside allowed directory: path=%s, allowed_prefix=%s", path, VIRTUAL_PATH_PREFIX)
            raise PermissionError(errno.EACCES, f"Access denied: path must be under '{VIRTUAL_PATH_PREFIX}'", path)

        resolved_path = self._resolve_path(path)
        max_download_size = 100 * 1024 * 1024  # 100MB 上限
        try:
            file_size = os.path.getsize(resolved_path)
            if file_size > max_download_size:
                raise OSError(errno.EFBIG, f"File exceeds maximum download size of {max_download_size} bytes", path)
            # TOCTOU 注意：文件可能在 getsize() 和 read() 之间增长；
            # 在受控的沙箱环境中这是可接受的权衡。
            with open(resolved_path, "rb") as f:
                return f.read()
        except OSError as e:
            # 使用原始虚拟路径重新抛出
            raise type(e)(e.errno, e.strerror, path) from None

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写入文件内容。

        执行流程：
        1. 解析虚拟路径为本地路径
        2. 检查是否为只读路径（只读则拒绝写入）
        3. 自动创建不存在的父目录
        4. 将内容中的容器路径解析为本地路径
        5. 写入文件
        6. 将路径添加到 agent-written 追踪集合

        Args:
            path: 文件的虚拟路径。
            content: 要写入的文本内容。
            append: 是否追加模式。

        Raises:
            OSError(EROFS): 如果路径在只读挂载下。
            OSError: 其他写入错误（使用虚拟路径）。
        """
        resolved = self._resolve_path_with_mapping(path)
        resolved_path = resolved.path
        # 只读路径检查
        if self._is_resolved_path_read_only(resolved):
            raise OSError(errno.EROFS, "Read-only file system", path)
        try:
            # 自动创建父目录
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            # 将内容中的容器路径解析为本地路径（使用内容专用的解析器，
            # 处理正斜杠安全）
            resolved_content = self._resolve_paths_in_content(content)
            mode = "a" if append else "w"
            with open(resolved_path, mode, encoding="utf-8") as f:
                f.write(resolved_content)
            # 追踪此路径，read_file 知道需要反向解析。
            # 只有 Agent 写入的文件才会被反向解析；用户上传和外部工具
            # 输出保持原样。
            self._agent_written_paths.add(resolved_path)
        except OSError as e:
            # 使用原始虚拟路径重新抛出
            raise type(e)(e.errno, e.strerror, path) from None

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """使用 glob 模式搜索文件路径。

        在解析后的本地路径下执行搜索，然后将结果中的本地路径反向解析为虚拟路径。

        Args:
            path: 搜索根目录的虚拟路径。
            pattern: glob 模式字符串。
            include_dirs: 是否包含目录。
            max_results: 最大结果数。

        Returns:
            ``(matches, truncated)`` 元组，matches 为虚拟路径列表。
        """
        resolved_path = Path(self._resolve_path(path))
        matches, truncated = find_glob_matches(resolved_path, pattern, include_dirs=include_dirs, max_results=max_results)
        # 将本地路径反向解析为虚拟路径
        return [self._reverse_resolve_path(match) for match in matches], truncated

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

        在解析后的本地路径下执行搜索，然后将结果中的本地路径反向解析为虚拟路径。

        Args:
            path: 搜索根目录的虚拟路径。
            pattern: 搜索模式。
            glob: 文件名过滤模式。
            literal: 是否为字面量搜索。
            case_sensitive: 是否区分大小写。
            max_results: 最大结果数。

        Returns:
            ``(matches, truncated)`` 元组，matches 中的路径为虚拟路径。
        """
        resolved_path = Path(self._resolve_path(path))
        matches, truncated = find_grep_matches(
            resolved_path,
            pattern,
            glob_pattern=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
        # 将结果中的本地路径反向解析为虚拟路径
        return [
            GrepMatch(
                path=self._reverse_resolve_path(match.path),
                line_number=match.line_number,
                line=match.line,
            )
            for match in matches
        ], truncated

    def update_file(self, path: str, content: bytes) -> None:
        """以二进制模式更新文件。

        与 write_file 不同，本方法直接写入原始字节数据，不做路径解析。
        仍会检查只读限制。

        Args:
            path: 文件的虚拟路径。
            content: 要写入的原始字节数据。

        Raises:
            OSError(EROFS): 如果路径在只读挂载下。
            OSError: 其他写入错误（使用虚拟路径）。
        """
        resolved = self._resolve_path_with_mapping(path)
        resolved_path = resolved.path
        # 只读路径检查
        if self._is_resolved_path_read_only(resolved):
            raise OSError(errno.EROFS, "Read-only file system", path)
        try:
            # 自动创建父目录
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(resolved_path, "wb") as f:
                f.write(content)
        except OSError as e:
            # 使用原始虚拟路径重新抛出
            raise type(e)(e.errno, e.strerror, path) from None
