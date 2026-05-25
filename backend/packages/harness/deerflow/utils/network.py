"""网络工具模块 —— 线程安全的端口分配器。

本模块为 DeerFlow 系统提供线程安全的网络端口管理能力。核心组件
``PortAllocator`` 维护一个已保留端口的集合，通过线程锁确保并发场景下
端口分配的原子性，避免多个组件或线程在同一时刻被分配到相同端口。

应用场景：
    - **Docker 容器端口映射** —— DeerFlow 使用沙箱容器执行用户代码，
      每个容器需要一个独立的宿主机端口进行端口映射。
    - **开发服务器启动** —— 自动为开发服务器分配可用端口，
      避免与已有服务冲突。
    - **并发测试** —— 在并行运行的测试用例中分配互不冲突的端口。

设计要点：
    - **线程安全** —— 使用 ``threading.Lock`` 保护端口集合的读写操作，
      确保同一进程中多个线程不会分配到相同端口。
    - **实际可用性检查** —— 不仅检查内部保留集合，还通过
      ``socket.bind("0.0.0.0", port)`` 验证端口在操作系统层面的可用性，
      避免与系统其他服务冲突。
    - **绑定地址选择** —— 使用 ``0.0.0.0``（通配地址）而非
      ``127.0.0.1``，因为 Docker 绑定到 ``0.0.0.0:PORT``，
      仅检查 localhost 可能产生假阴性（误报端口可用）。
    - **上下文管理器支持** —— ``allocate_context`` 方法提供自动释放语义，
      推荐在 ``with`` 语句中使用，避免端口泄漏。

模块级便捷函数：
    - :func:`get_free_port` —— 通过全局分配器获取可用端口
    - :func:`release_port` —— 通过全局分配器释放端口
"""

import socket
import threading
from contextlib import contextmanager


class PortAllocator:
    """线程安全的端口分配器，防止并发环境下的端口冲突。

    本类维护一个已保留端口的集合，使用线程锁确保端口分配的原子性。
    端口一旦分配，会保持保留状态直到显式释放。

    端口可用性检查包含两层：
    1. 内部保留集合检查 —— 避免同一分配器重复分配。
    2. 操作系统层面检查 —— 通过 ``socket.bind`` 验证端口未被其他进程占用。

    用法示例::

        allocator = PortAllocator()

        # 方式一：手动分配和释放
        port = allocator.allocate(start_port=8080)
        try:
            # 使用端口...
            pass
        finally:
            allocator.release(port)

        # 方式二：上下文管理器（推荐）
        with allocator.allocate_context(start_port=8080) as port:
            # 使用端口...
            pass  # 离开 with 块时自动释放

    Attributes:
        _lock (threading.Lock): 保护端口集合操作的线程锁。
        _reserved_ports (set[int]): 已保留的端口号集合。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reserved_ports: set[int] = set()

    def _is_port_available(self, port: int) -> bool:
        """检查端口是否可以绑定。

        执行两层检查：
        1. 端口是否已在内部保留集合中（避免重复分配）。
        2. 端口在操作系统层面是否可绑定（通过 socket.bind 测试）。

        绑定到 ``0.0.0.0``（通配地址）而非 ``127.0.0.1`` 的原因：
        Docker 绑定到 ``0.0.0.0:PORT``，如果仅检查 ``127.0.0.1``,
        可能会误报端口为可用，而实际上 Docker 已经在通配地址上占用了该端口。

        Args:
            port: 待检查的端口号。

        Returns:
            ``True`` 如果端口可以绑定，``False`` 如果已被占用。
        """
        if port in self._reserved_ports:
            return False

        # 绑定到 0.0.0.0（通配地址）而非 localhost，以准确反映
        # Docker 的绑定行为 —— Docker 绑定到 0.0.0.0:PORT；
        # 仅检查 127.0.0.1 可能在 Docker 已占用通配地址时
        # 误报端口为可用
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def allocate(self, start_port: int = 8080, max_range: int = 100) -> int:
        """线程安全地分配一个可用端口。

        从 ``start_port`` 开始线性扫描，找到第一个可通过两层检查的端口，
        将其加入保留集合并返回。整个扫描和保留操作在锁的保护下执行，
        确保并发调用不会返回相同端口。

        Args:
            start_port: 扫描起始端口号（默认 8080）。
            max_range: 最大扫描范围（默认 100 个端口），
                即扫描 ``[start_port, start_port + max_range)`` 区间。

        Returns:
            可用的端口号（已加入保留集合）。

        Raises:
            RuntimeError: 在指定范围内未找到可用端口时抛出。
        """
        with self._lock:
            for port in range(start_port, start_port + max_range):
                if self._is_port_available(port):
                    self._reserved_ports.add(port)
                    return port

            raise RuntimeError(f"No available port found in range {start_port}-{start_port + max_range}")

    def release(self, port: int) -> None:
        """释放之前分配的端口，使其可被再次分配。

        如果端口不在保留集合中，操作会静默成功（使用 ``discard`` 而非 ``remove``），
        这使得释放操作具有幂等性，简化了调用者的错误处理。

        Args:
            port: 要释放的端口号。
        """
        with self._lock:
            self._reserved_ports.discard(port)

    @contextmanager
    def allocate_context(self, start_port: int = 8080, max_range: int = 100):
        """端口分配的上下文管理器，支持自动释放。

        进入上下文时分配端口，退出时自动释放（即使发生异常也会释放）。
        这是推荐的端口使用方式，可以有效防止端口泄漏。

        Args:
            start_port: 扫描起始端口号（默认 8080）。
            max_range: 最大扫描范围（默认 100 个端口）。

        Yields:
            可用的端口号。

        Example::

            with allocator.allocate_context(start_port=9000) as port:
                server.start(port=port)
                # server 关闭后，端口自动释放
        """
        port = self.allocate(start_port, max_range)
        try:
            yield port
        finally:
            # 即使 with 块内发生异常，也确保端口被释放
            self.release(port)


# 全局端口分配器实例，供整个应用共享使用。
# 使用全局实例可以跨模块协调端口分配，避免不同模块各自维护分配器
# 导致端口冲突（因为各自的保留集合互不可见）。
_global_port_allocator = PortAllocator()


def get_free_port(start_port: int = 8080, max_range: int = 100) -> int:
    """通过全局分配器获取一个可用端口（线程安全）。

    这是 ``PortAllocator.allocate`` 的便捷包装，使用全局分配器实例。
    并发调用此函数不会返回相同端口。分配的端口会保持保留状态，
    直到调用 ``release_port`` 释放。

    Args:
        start_port: 扫描起始端口号（默认 8080）。
        max_range: 最大扫描范围（默认 100 个端口）。

    Returns:
        可用的端口号。

    Raises:
        RuntimeError: 在指定范围内未找到可用端口。

    Note:
        调用者有责任在端口使用完毕后调用 ``release_port`` 释放。
        推荐使用 ``try/finally`` 模式确保释放。
    """
    return _global_port_allocator.allocate(start_port, max_range)


def release_port(port: int) -> None:
    """释放之前通过 ``get_free_port`` 分配的端口。

    Args:
        port: 要释放的端口号。
    """
    _global_port_allocator.release(port)
