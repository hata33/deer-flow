"""线程安全的网络端口分配工具。

提供 PortAllocator 类和全局快捷函数，用于在并发环境中
安全地分配和释放端口，防止端口冲突。

典型场景：Docker 容器端口映射、测试服务端口分配。
"""

import socket
import threading
from contextlib import contextmanager


class PortAllocator:
    """线程安全的端口分配器，防止并发环境下的端口冲突。

    维护一个已保留端口集合，通过锁确保分配操作的原子性。
    端口分配后保持保留状态，直到显式释放。

    支持两种使用方式：
    - 手动 allocate/release
    - 上下文管理器 allocate_context（推荐，自动释放）
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reserved_ports: set[int] = set()

    def _is_port_available(self, port: int) -> bool:
        """检查端口是否可用（未被保留且未被占用）。

        绑定到 0.0.0.0（通配地址）而非 localhost，确保与 Docker 的
        绑定行为一致——Docker 绑定 0.0.0.0:PORT，仅检查 127.0.0.1
        可能误报端口为可用。

        Args:
            port: 待检查的端口号。

        Returns:
            端口可用时返回 True。
        """
        if port in self._reserved_ports:
            return False

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def allocate(self, start_port: int = 8080, max_range: int = 100) -> int:
        """线程安全地分配一个可用端口。

        从 start_port 开始搜索，找到第一个可用端口后标记为保留。
        端口保持保留状态直到调用 release()。

        Args:
            start_port: 搜索起始端口号。
            max_range: 最大搜索范围。

        Returns:
            可用的端口号。

        Raises:
            RuntimeError: 指定范围内无可用端口。
        """
        with self._lock:
            for port in range(start_port, start_port + max_range):
                if self._is_port_available(port):
                    self._reserved_ports.add(port)
                    return port

            raise RuntimeError(f"No available port found in range {start_port}-{start_port + max_range}")

    def release(self, port: int) -> None:
        """释放之前分配的端口。

        Args:
            port: 待释放的端口号。
        """
        with self._lock:
            self._reserved_ports.discard(port)

    @contextmanager
    def allocate_context(self, start_port: int = 8080, max_range: int = 100):
        """端口分配的上下文管理器，退出时自动释放。

        Args:
            start_port: 搜索起始端口号。
            max_range: 最大搜索范围。

        Yields:
            可用的端口号。
        """
        port = self.allocate(start_port, max_range)
        try:
            yield port
        finally:
            self.release(port)


# 全局端口分配器实例
_global_port_allocator = PortAllocator()


def get_free_port(start_port: int = 8080, max_range: int = 100) -> int:
    """获取一个空闲端口（线程安全），端口保持保留直到调用 release_port()。

    Args:
        start_port: 搜索起始端口号。
        max_range: 最大搜索范围。

    Returns:
        可用的端口号。

    Raises:
        RuntimeError: 指定范围内无可用端口。
    """
    return _global_port_allocator.allocate(start_port, max_range)


def release_port(port: int) -> None:
    """释放之前通过 get_free_port 分配的端口。

    Args:
        port: 待释放的端口号。
    """
    _global_port_allocator.release(port)
