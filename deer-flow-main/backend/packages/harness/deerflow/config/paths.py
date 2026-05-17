"""路径管理模块。

本模块集中管理 DeerFlow 的所有数据目录布局，
包括线程数据、沙箱目录、记忆文件、智能体目录等。
支持虚拟路径（/mnt/user-data/）到宿主机路径的映射。

核心概念：
    - **BaseDir** — 所有应用数据的根目录，支持多种解析优先级。
    - **虚拟路径** — 沙箱内代理看到的路径（如 /mnt/user-data/workspace/）。
    - **宿主路径** — 宿主机上的实际文件系统路径。
    - **Host BaseDir** — Docker DooD 模式下的宿主机侧基础目录。

目录布局（宿主机侧）：
    {base_dir}/
    ├── memory.json                  # 全局记忆数据
    ├── USER.md                      # 全局用户档案（注入到所有代理）
    ├── agents/
    │   └── {agent_name}/
    │       ├── config.yaml          # 智能体配置
    │       ├── SOUL.md              # 智能体人格/身份定义
    │       └── memory.json          # 智能体专属记忆
    └── threads/
        └── {thread_id}/
            ├── acp-workspace/       # ACP 代理工作空间
            └── user-data/           # 挂载为沙箱内的 /mnt/user-data/
                ├── workspace/       # /mnt/user-data/workspace/
                ├── uploads/         # /mnt/user-data/uploads/
                └── outputs/         # /mnt/user-data/outputs/

BaseDir 解析优先级：
    1. 构造函数参数 base_dir
    2. DEER_FLOW_HOME 环境变量
    3. 本地开发回退：cwd/.deer-flow（当 cwd 是 backend/ 目录时）
    4. 默认：$HOME/.deer-flow

Host BaseDir（Docker DooD 模式）：
    当在 Docker 容器中运行并挂载 Docker socket 时（DooD），
    Docker 守护进程在宿主机上运行，会使用宿主机路径解析卷挂载。
    通过 DEER_FLOW_HOST_BASE_DIR 环境变量设置宿主机侧的基础路径。
"""
import os
import re
import shutil
from pathlib import Path

# 沙箱内代理看到的虚拟路径前缀
VIRTUAL_PATH_PREFIX = "/mnt/user-data"

# 线程 ID 安全字符模式：仅允许字母、数字、下划线、连字符
_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class Paths:
    """DeerFlow 应用数据路径集中管理器。

    提供所有数据目录的宿主机路径和虚拟路径映射。
    所有路径属性返回 pathlib.Path 对象。

    用法：
        paths = get_paths()             # 获取全局单例
        workspace = paths.sandbox_work_dir("thread-123")  # 宿主机工作空间路径
        virtual = "/mnt/user-data/outputs/report.pdf"
        actual = paths.resolve_virtual_path("thread-123", virtual)  # 解析为宿主路径
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def host_base_dir(self) -> Path:
        """Docker DooD 模式下的宿主机侧基础目录。

        当在 Docker 容器中运行并挂载 Docker socket（DooD 模式）时，
        Docker 守护进程在宿主机上运行，需要使用宿主机路径解析卷挂载。

        设置 DEER_FLOW_HOST_BASE_DIR 环境变量为容器 base_dir 对应的宿主机路径。
        未设置时回退到 base_dir（本地/原生执行环境）。
        """
        if env := os.getenv("DEER_FLOW_HOST_BASE_DIR"):
            return Path(env)
        return self.base_dir

    @property
    def base_dir(self) -> Path:
        """应用数据根目录。

        解析优先级：
        1. 构造函数参数 base_dir
        2. DEER_FLOW_HOME 环境变量
        3. 本地开发回退：cwd/.deer-flow（当 cwd 是 backend/ 目录时）
        4. 默认：$HOME/.deer-flow
        """
        if self._base_dir is not None:
            return self._base_dir

        if env_home := os.getenv("DEER_FLOW_HOME"):
            return Path(env_home).resolve()

        cwd = Path.cwd()
        # 如果当前目录是 backend/ 或包含 pyproject.toml，使用 cwd/.deer-flow
        if cwd.name == "backend" or (cwd / "pyproject.toml").exists():
            return cwd / ".deer-flow"

        return Path.home() / ".deer-flow"

    @property
    def memory_file(self) -> Path:
        """全局记忆文件路径：{base_dir}/memory.json"""
        return self.base_dir / "memory.json"

    @property
    def user_md_file(self) -> Path:
        """全局用户档案文件路径：{base_dir}/USER.md

        该文件内容会被注入到所有代理的系统提示词中。
        """
        return self.base_dir / "USER.md"

    @property
    def agents_dir(self) -> Path:
        """自定义智能体根目录：{base_dir}/agents/"""
        return self.base_dir / "agents"

    def agent_dir(self, name: str) -> Path:
        """指定智能体的目录：{base_dir}/agents/{name}/

        名称会被转换为小写。
        """
        return self.agents_dir / name.lower()

    def agent_memory_file(self, name: str) -> Path:
        """指定智能体的记忆文件：{base_dir}/agents/{name}/memory.json"""
        return self.agent_dir(name) / "memory.json"

    def thread_dir(self, thread_id: str) -> Path:
        """线程数据目录：{base_dir}/threads/{thread_id}/

        该目录包含 user-data/ 子目录，会被挂载到沙箱内的 /mnt/user-data/。

        Args:
            thread_id: 线程 ID，仅允许字母、数字、连字符、下划线。

        Raises:
            ValueError: thread_id 包含不安全字符（路径分隔符或 ..）。
        """
        if not _SAFE_THREAD_ID_RE.match(thread_id):
            raise ValueError(f"Invalid thread_id {thread_id!r}: only alphanumeric characters, hyphens, and underscores are allowed.")
        return self.base_dir / "threads" / thread_id

    def sandbox_work_dir(self, thread_id: str) -> Path:
        """沙箱工作空间目录（宿主机路径）。

        宿主机：{base_dir}/threads/{thread_id}/user-data/workspace/
        沙箱内：/mnt/user-data/workspace/
        """
        return self.thread_dir(thread_id) / "user-data" / "workspace"

    def sandbox_uploads_dir(self, thread_id: str) -> Path:
        """用户上传文件目录（宿主机路径）。

        宿主机：{base_dir}/threads/{thread_id}/user-data/uploads/
        沙箱内：/mnt/user-data/uploads/
        """
        return self.thread_dir(thread_id) / "user-data" / "uploads"

    def sandbox_outputs_dir(self, thread_id: str) -> Path:
        """代理生成产物目录（宿主机路径）。

        宿主机：{base_dir}/threads/{thread_id}/user-data/outputs/
        沙箱内：/mnt/user-data/outputs/
        """
        return self.thread_dir(thread_id) / "user-data" / "outputs"

    def acp_workspace_dir(self, thread_id: str) -> Path:
        """ACP 代理工作空间目录（宿主机路径）。

        宿主机：{base_dir}/threads/{thread_id}/acp-workspace/
        沙箱内：/mnt/acp-workspace/

        每个线程拥有独立的 ACP 工作空间，确保并发会话不能互相读取 ACP 代理输出。
        """
        return self.thread_dir(thread_id) / "acp-workspace"

    def sandbox_user_data_dir(self, thread_id: str) -> Path:
        """用户数据根目录（宿主机路径）。

        宿主机：{base_dir}/threads/{thread_id}/user-data/
        沙箱内：/mnt/user-data/
        """
        return self.thread_dir(thread_id) / "user-data"

    def ensure_thread_dirs(self, thread_id: str) -> None:
        """为线程创建所有标准沙箱目录。

        创建的目录包括 workspace、uploads、outputs 和 acp-workspace。
        目录权限设为 0o777，确保沙箱容器（可能以不同的 UID 运行）
        可以写入卷挂载的路径而不会出现 "Permission denied" 错误。

        显式调用 chmod() 是必要的，因为 Path.mkdir(mode=...) 受进程
        umask 影响可能不会产生预期的权限。
        """
        for d in [
            self.sandbox_work_dir(thread_id),
            self.sandbox_uploads_dir(thread_id),
            self.sandbox_outputs_dir(thread_id),
            self.acp_workspace_dir(thread_id),
        ]:
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o777)

    def delete_thread_dir(self, thread_id: str) -> None:
        """删除线程的所有持久化数据。

        幂等操作：不存在的线程目录会被忽略。
        """
        thread_dir = self.thread_dir(thread_id)
        if thread_dir.exists():
            shutil.rmtree(thread_dir)

    def resolve_virtual_path(self, thread_id: str, virtual_path: str) -> Path:
        """将沙箱虚拟路径解析为宿主机实际文件系统路径。

        用于将代理在沙箱内看到的路径（如 /mnt/user-data/outputs/report.pdf）
        转换为宿主机上的实际路径，以便读取或操作文件。

        安全检查：
        - 路径必须以 /mnt/user-data/ 开头（精确段匹配）
        - 解析后的路径不能逃逸 user-data 目录（防止路径遍历攻击）

        Args:
            thread_id: 线程 ID。
            virtual_path: 沙箱内的虚拟路径（如 /mnt/user-data/outputs/report.pdf）。

        Returns:
            解析后的宿主机绝对路径。

        Raises:
            ValueError: 路径不以预期的虚拟前缀开头或检测到路径遍历攻击。
        """
        stripped = virtual_path.lstrip("/")
        prefix = VIRTUAL_PATH_PREFIX.lstrip("/")

        # 要求精确的段边界匹配，避免前缀混淆（如拒绝 "mnt/user-dataX/..."）
        if stripped != prefix and not stripped.startswith(prefix + "/"):
            raise ValueError(f"Path must start with /{prefix}")

        relative = stripped[len(prefix) :].lstrip("/")
        base = self.sandbox_user_data_dir(thread_id).resolve()
        actual = (base / relative).resolve()

        # 防止路径遍历攻击：解析后的路径必须在 user-data 目录下
        try:
            actual.relative_to(base)
        except ValueError:
            raise ValueError("Access denied: path traversal detected")

        return actual


# ── 全局单例 ──────────────────────────────────────────────────────────────

_paths: Paths | None = None


def get_paths() -> Paths:
    """获取全局 Paths 单例（延迟初始化）。"""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths


def resolve_path(path: str) -> Path:
    """将路径解析为绝对 Path。

    相对路径相对于应用基础目录（base_dir）解析。
    绝对路径直接返回（规范化后）。

    Args:
        path: 待解析的路径字符串。

    Returns:
        解析后的绝对 Path。
    """
    p = Path(path)
    if not p.is_absolute():
        p = get_paths().base_dir / path
    return p.resolve()
