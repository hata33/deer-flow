"""本地沙箱实现。

基于宿主机文件系统，通过虚拟路径映射（container path ↔ local path）实现隔离。
Agent 看到 /mnt/user-data/... 等容器路径，实际操作映射到宿主机本地路径。
"""

import ntpath
import os
import shutil
import subprocess
from pathlib import Path

from deerflow.sandbox.local.list_dir import list_dir
from deerflow.sandbox.sandbox import Sandbox


class LocalSandbox(Sandbox):
    """本地沙箱：在宿主机上直接执行命令和文件操作，通过路径映射实现虚拟化。"""

    @staticmethod
    def _shell_name(shell: str) -> str:
        """提取 shell 可执行文件名（去除路径，统一小写）。"""
        return shell.replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _is_powershell(shell: str) -> bool:
        """判断是否为 PowerShell（含 pwsh）。"""
        return LocalSandbox._shell_name(shell) in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}

    @staticmethod
    def _is_cmd_shell(shell: str) -> bool:
        """判断是否为 cmd.exe。"""
        return LocalSandbox._shell_name(shell) in {"cmd", "cmd.exe"}

    @staticmethod
    def _find_first_available_shell(candidates: tuple[str, ...]) -> str | None:
        """从候选列表中找到第一个可用的 shell（支持绝对路径和 PATH 查找）。"""
        for shell in candidates:
            if os.path.isabs(shell):
                # 绝对路径：直接检查文件是否存在且可执行
                if os.path.isfile(shell) and os.access(shell, os.X_OK):
                    return shell
                continue

            # 非绝对路径：通过 PATH 环境变量查找
            shell_from_path = shutil.which(shell)
            if shell_from_path is not None:
                return shell_from_path

        return None

    def __init__(self, id: str, path_mappings: dict[str, str] | None = None):
        """初始化本地沙箱。

        Args:
            id: 沙箱标识符。
            path_mappings: 容器路径到本地路径的映射字典。
                           例：{"/mnt/skills": "/absolute/path/to/skills"}
        """
        super().__init__(id)
        self.path_mappings = path_mappings or {}

    def _resolve_path(self, path: str) -> str:
        """将容器路径解析为本地路径（正向映射）。

        按映射键长度降序匹配，确保更具体的路径优先。
        例：/mnt/skills/python → /home/user/skills/python
        """
        path_str = str(path)

        # 按容器路径长度降序排列，优先匹配更具体的前缀
        for container_path, local_path in sorted(self.path_mappings.items(), key=lambda x: len(x[0]), reverse=True):
            if path_str == container_path or path_str.startswith(container_path + "/"):
                relative = path_str[len(container_path) :].lstrip("/")
                resolved = str(Path(local_path) / relative) if relative else local_path
                return resolved

        # 无匹配映射，返回原始路径
        return path_str

    def _reverse_resolve_path(self, path: str) -> str:
        """将本地路径反向解析为容器路径（逆向映射）。

        按本地路径长度降序匹配，用于将输出中的真实路径还原为虚拟路径。
        例：/home/user/skills/python → /mnt/skills/python
        """
        path_str = str(Path(path).resolve())

        # 按本地路径长度降序排列，优先匹配更具体的前缀
        for container_path, local_path in sorted(self.path_mappings.items(), key=lambda x: len(x[1]), reverse=True):
            local_path_resolved = str(Path(local_path).resolve())
            if path_str.startswith(local_path_resolved):
                relative = path_str[len(local_path_resolved) :].lstrip("/")
                resolved = f"{container_path}/{relative}" if relative else container_path
                return resolved

        # 无匹配映射，返回原始路径
        return path_str

    def _reverse_resolve_paths_in_output(self, output: str) -> str:
        """将输出文本中的本地路径批量替换为容器路径。

        使用正则匹配绝对路径，按本地路径长度降序替换以避免部分匹配。
        """
        import re

        # 按本地路径长度降序排列，确保长路径优先匹配
        sorted_mappings = sorted(self.path_mappings.items(), key=lambda x: len(x[1]), reverse=True)

        if not sorted_mappings:
            return output

        result = output
        for container_path, local_path in sorted_mappings:
            local_path_resolved = str(Path(local_path).resolve())
            escaped_local = re.escape(local_path_resolved)
            # 匹配本地路径后跟可选的路径组件（不含空白和特殊字符）
            pattern = re.compile(escaped_local + r"(?:/[^\s\"';&|<>()]*)?")

            def replace_match(match: re.Match) -> str:
                matched_path = match.group(0)
                return self._reverse_resolve_path(matched_path)

            result = pattern.sub(replace_match, result)

        return result

    def _resolve_paths_in_command(self, command: str) -> str:
        """将命令中的容器路径批量替换为本地路径。

        使用前瞻断言确保在路径段边界处匹配，避免 /mnt/skills 匹配 /mnt/skills-extra。
        """
        import re

        # 按容器路径长度降序排列
        sorted_mappings = sorted(self.path_mappings.items(), key=lambda x: len(x[0]), reverse=True)

        if not sorted_mappings:
            return command

        # 前瞻断言确保只在路径段边界匹配，防止子串误匹配
        patterns = [re.escape(container_path) + r"(?=/|$|[\s\"';&|<>()])(?:/[^\s\"';&|<>()]*)?" for container_path, _ in sorted_mappings]
        pattern = re.compile("|".join(f"({p})" for p in patterns))

        def replace_match(match: re.Match) -> str:
            matched_path = match.group(0)
            return self._resolve_path(matched_path)

        return pattern.sub(replace_match, command)

    @staticmethod
    def _get_shell() -> str:
        """检测可用的 shell 可执行文件，按优先级回退。"""
        # 优先尝试 Unix shell
        shell = LocalSandbox._find_first_available_shell(("/bin/zsh", "/bin/bash", "/bin/sh", "sh"))
        if shell is not None:
            return shell

        # Windows 环境回退到 PowerShell 或 cmd
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
        """执行命令：先解析容器路径，再调用本地 shell 执行，最后将输出中的路径还原。"""
        # 正向映射：容器路径 → 本地路径
        resolved_command = self._resolve_paths_in_command(command)
        shell = self._get_shell()

        if os.name == "nt":
            # Windows：根据 shell 类型构造不同的参数
            if self._is_powershell(shell):
                args = [shell, "-NoProfile", "-Command", resolved_command]
            elif self._is_cmd_shell(shell):
                args = [shell, "/c", resolved_command]
            else:
                args = [shell, "-c", resolved_command]

            result = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
        else:
            # Unix：使用 shell=True 执行
            result = subprocess.run(
                resolved_command,
                executable=shell,
                shell=True,
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
        # 逆向映射：输出中的本地路径 → 容器路径
        return self._reverse_resolve_paths_in_output(final_output)

    def list_dir(self, path: str, max_depth=2) -> str:
        """列出目录内容，路径经过正反向映射。"""
        resolved_path = self._resolve_path(path)
        entries = list_dir(resolved_path, max_depth)
        # 将结果中的本地路径还原为容器路径
        return [self._reverse_resolve_paths_in_output(entry) for entry in entries]

    def read_file(self, path: str) -> str:
        """读取文件内容，OSError 时用原始路径抛出（隐藏内部映射路径）。"""
        resolved_path = self._resolve_path(path)
        try:
            with open(resolved_path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            # 用原始容器路径重新抛出，避免暴露本地文件系统结构
            raise type(e)(e.errno, e.strerror, path) from None

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写入文件（自动创建父目录），OSError 时用原始路径抛出。"""
        resolved_path = self._resolve_path(path)
        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            mode = "a" if append else "w"
            with open(resolved_path, mode, encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            raise type(e)(e.errno, e.strerror, path) from None

    def update_file(self, path: str, content: bytes) -> None:
        """以二进制模式更新文件（自动创建父目录），OSError 时用原始路径抛出。"""
        resolved_path = self._resolve_path(path)
        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(resolved_path, "wb") as f:
                f.write(content)
        except OSError as e:
            raise type(e)(e.errno, e.strerror, path) from None
