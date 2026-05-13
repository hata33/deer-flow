"""AIO 沙箱实现。

通过 HTTP API 连接运行中的 AIO 沙箱容器（agent-infra/sandbox），
实现命令执行、文件读写和目录列表等沙箱操作。
"""

import base64
import logging

from agent_sandbox import Sandbox as AioSandboxClient

from deerflow.sandbox.sandbox import Sandbox

logger = logging.getLogger(__name__)


class AioSandbox(Sandbox):
    """基于 AIO Docker 容器的沙箱实现，通过 HTTP API 交互。"""

    def __init__(self, id: str, base_url: str, home_dir: str | None = None):
        """初始化 AIO 沙箱。

        Args:
            id: 沙箱唯一标识。
            base_url: 沙箱 API 地址（如 http://localhost:8080）。
            home_dir: 沙箱内主目录，None 时从沙箱自动获取。
        """
        super().__init__(id)
        self._base_url = base_url
        self._client = AioSandboxClient(base_url=base_url, timeout=600)
        self._home_dir = home_dir

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def home_dir(self) -> str:
        """获取沙箱内主目录（懒加载）。"""
        if self._home_dir is None:
            context = self._client.sandbox.get_context()
            self._home_dir = context.home_dir
        return self._home_dir

    def execute_command(self, command: str) -> str:
        """在沙箱内执行 shell 命令。"""
        try:
            result = self._client.shell.exec_command(command=command)
            output = result.data.output if result.data else ""
            return output if output else "(no output)"
        except Exception as e:
            logger.error(f"Failed to execute command in sandbox: {e}")
            return f"Error: {e}"

    def read_file(self, path: str) -> str:
        """读取沙箱内文件内容。"""
        try:
            result = self._client.file.read_file(file=path)
            return result.data.content if result.data else ""
        except Exception as e:
            logger.error(f"Failed to read file in sandbox: {e}")
            return f"Error: {e}"

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """列出沙箱内目录内容（通过 find 命令，限制深度和条目数）。"""
        try:
            result = self._client.shell.exec_command(command=f"find {path} -maxdepth {max_depth} -type f -o -type d 2>/dev/null | head -500")
            output = result.data.output if result.data else ""
            if output:
                return [line.strip() for line in output.strip().split("\n") if line.strip()]
            return []
        except Exception as e:
            logger.error(f"Failed to list directory in sandbox: {e}")
            return []

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写入文件（append 模式先读取已有内容再拼接）。"""
        try:
            if append:
                existing = self.read_file(path)
                if not existing.startswith("Error:"):
                    content = existing + content
            self._client.file.write_file(file=path, content=content)
        except Exception as e:
            logger.error(f"Failed to write file in sandbox: {e}")
            raise

    def update_file(self, path: str, content: bytes) -> None:
        """以二进制模式更新文件（base64 编码传输）。"""
        try:
            base64_content = base64.b64encode(content).decode("utf-8")
            self._client.file.write_file(file=path, content=base64_content, encoding="base64")
        except Exception as e:
            logger.error(f"Failed to update file in sandbox: {e}")
            raise
