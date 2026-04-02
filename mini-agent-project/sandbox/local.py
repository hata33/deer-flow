"""
本地文件系统沙箱

使用本地文件系统作为隔离环境
"""
import logging
import subprocess
from pathlib import Path
from typing import Self

from .base import Sandbox, SandboxProvider
from utils import generate_id

logger = logging.getLogger(__name__)


class LocalSandbox(Sandbox):
    """
    本地文件系统沙箱

    使用指定目录作为隔离环境
    """

    def __init__(self, id: str, work_dir: Path):
        super().__init__(id)
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def execute_command(self, command: str) -> str:
        """在沙箱中执行命令"""
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.work_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )

            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"

            return output

        except subprocess.TimeoutExpired:
            return f"命令执行超时: {command}"
        except Exception as e:
            return f"命令执行错误: {e}"

    def read_file(self, path: str) -> str:
        """读取文件内容"""
        file_path = self._resolve_path(path)

        try:
            with open(file_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return f"文件不存在: {path}"
        except Exception as e:
            return f"读取文件错误: {e}"

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写入文件"""
        file_path = self._resolve_path(path)

        try:
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)

            mode = "a" if append else "w"
            with open(file_path, mode, encoding="utf-8") as f:
                f.write(content)

        except Exception as e:
            raise IOError(f"写入文件错误: {e}")

    def list_dir(self, path: str = ".", max_depth: int = 2) -> list[str]:
        """列出目录内容"""
        dir_path = self._resolve_path(path)

        if not dir_path.exists():
            return [f"目录不存在: {path}"]

        try:
            result = []

            def _list_recursive(current_path: Path, depth: int = 0):
                if depth > max_depth:
                    return

                try:
                    for item in current_path.iterdir():
                        relative_path = item.relative_to(self.work_dir)
                        result.append(str(relative_path))

                        if item.is_dir() and depth < max_depth:
                            _list_recursive(item, depth + 1)
                except PermissionError:
                    pass

            _list_recursive(dir_path)
            return sorted(result)

        except Exception as e:
            return [f"列出目录错误: {e}"]

    def _resolve_path(self, path: str) -> Path:
        """解析路径，确保在沙箱内"""
        # 绝对路径
        if Path(path).is_absolute():
            return Path(path)

        # 相对路径，相对于工作目录
        return self.work_dir / path


class LocalSandboxProvider(SandboxProvider):
    """
    本地沙箱提供者

    管理本地文件系统沙箱的生命周期
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path.cwd() / "sandboxes"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._sandboxes: dict[str, LocalSandbox] = {}

    async def acquire(self, sandbox_id: str | None = None) -> LocalSandbox:
        """获取或创建沙箱"""
        if sandbox_id is None:
            sandbox_id = generate_id("sandbox")

        if sandbox_id not in self._sandboxes:
            work_dir = self.base_dir / sandbox_id
            self._sandboxes[sandbox_id] = LocalSandbox(sandbox_id, work_dir)
            logger.info(f"创建沙箱: {sandbox_id}")

        return self._sandboxes[sandbox_id]

    async def release(self, sandbox_id: str) -> None:
        """释放沙箱"""
        if sandbox_id in self._sandboxes:
            del self._sandboxes[sandbox_id]
            logger.info(f"释放沙箱: {sandbox_id}")

    def get(self, sandbox_id: str) -> LocalSandbox | None:
        """获取现有沙箱"""
        return self._sandboxes.get(sandbox_id)

    async def cleanup(self, sandbox_id: str) -> None:
        """清理沙箱数据"""
        if sandbox_id in self._sandboxes:
            sandbox = self._sandboxes[sandbox_id]
            # 删除工作目录
            import shutil
            if sandbox.work_dir.exists():
                shutil.rmtree(sandbox.work_dir)
            await self.release(sandbox_id)
            logger.info(f"清理沙箱: {sandbox_id}")
