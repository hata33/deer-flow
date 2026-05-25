"""集中式路径管理 — 文件系统路径的统一入口。

本模块管理 DeerFlow 所有文件系统路径的解析和构建。
它确保路径在不同运行环境（本地、Docker、DooD）下都能正确解析。

## 目录布局

```
{base_dir}/
├── memory.json              ← 全局记忆文件
├── USER.md                  ← 全局用户画像（注入到所有代理）
├── agents/                  ← 旧布局：共享的自定义代理（只读回退）
│   └── {agent_name}/
│       ├── config.yaml
│       ├── SOUL.md
│       └── memory.json
├── users/
│   └── {user_id}/
│       ├── memory.json      ← 按用户记忆
│       ├── agents/          ← 按用户的自定义代理
│       │   └── {agent_name}/
│       │       ├── config.yaml
│       │       ├── SOUL.md
│       │       └── memory.json
│       └── threads/
│           └── {thread_id}/
│               ├── user-data/       ← 挂载为 /mnt/user-data/
│               │   ├── workspace/   ← /mnt/user-data/workspace/
│               │   ├── uploads/     ← /mnt/user-data/uploads/
│               │   └── outputs/     ← /mnt/user-data/outputs/
│               └── acp-workspace/   ← /mnt/acp-workspace/
└── threads/                  ← 旧布局：无用户隔离的线程（只读回退）
    └── {thread_id}/
        └── ...
```

## BaseDir 解析优先级
1. 构造函数参数 base_dir
2. DEER_FLOW_HOME 环境变量
3. runtime_home() → {project_root}/.deer-flow

## Docker/DooD 支持
在 Docker-out-of-Docker（DooD）模式下，Docker 守护进程在宿主机上运行，
需要使用宿主机路径来创建卷挂载。DEER_FLOW_HOST_BASE_DIR 环境变量
指定容器内 base_dir 对应的宿主机路径。

## 安全
- thread_id 和 user_id 通过正则验证，只允许字母、数字、连字符、下划线
- resolve_virtual_path 检测路径遍历攻击
"""

import os
import re
import shutil
from pathlib import Path, PureWindowsPath

from deerflow.config.runtime_paths import runtime_home

# 沙箱内代理看到的虚拟路径前缀
VIRTUAL_PATH_PREFIX = "/mnt/user-data"

# ID 安全正则：只允许字母、数字、连字符、下划线
_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _default_local_base_dir() -> Path:
    """返回调用方项目的可写 DeerFlow 状态目录。"""
    return runtime_home()


def _validate_thread_id(thread_id: str) -> str:
    """验证线程 ID，防止路径遍历攻击。"""
    if not _SAFE_THREAD_ID_RE.match(thread_id):
        raise ValueError(f"Invalid thread_id {thread_id!r}: only alphanumeric characters, hyphens, and underscores are allowed.")
    return thread_id


def _validate_user_id(user_id: str) -> str:
    """验证用户 ID，防止路径遍历攻击。"""
    if not _SAFE_USER_ID_RE.match(user_id):
        raise ValueError(f"Invalid user_id {user_id!r}: only alphanumeric characters, hyphens, and underscores are allowed.")
    return user_id


def _join_host_path(base: str, *parts: str) -> str:
    """拼接宿主机文件系统路径，保留原始路径风格。

    Docker Desktop on Windows 要求绑定挂载源保持 Windows 路径格式
    （如 C:\\repo\\backend\\.deer-flow）。如果使用 POSIX Path 拼接，
    可能会意外产生混合分隔符。此辅助函数保留原始风格。

    检测规则：
    - 以盘符开头（C:\）→ Windows 路径
    - 以 UNC 前缀（\\）→ Windows 路径
    - 包含反斜杠 → Windows 路径
    - 其他 → POSIX 路径
    """
    if not parts:
        return base

    if re.match(r"^[A-Za-z]:[\\/]", base) or base.startswith("\\\\") or "\\" in base:
        result = PureWindowsPath(base)
        for part in parts:
            result /= part
        return str(result)

    result = Path(base)
    for part in parts:
        result /= part
    return str(result)


def join_host_path(base: str, *parts: str) -> str:
    """拼接宿主机文件系统路径（公开接口）。"""
    return _join_host_path(base, *parts)


class Paths:
    """DeerFlow 应用数据的集中式路径配置。

    所有路径通过 base_dir 为根推导。提供两类路径：
    - *属性/方法*: 返回 Path 对象，用于宿主机本地文件操作
    - *host_* 方法: 返回字符串，用于 Docker 卷挂载（保留 Windows 路径格式）

    参数:
        base_dir: 显式指定基础目录。None 时使用环境变量或默认值。
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def host_base_dir(self) -> Path:
        """宿主机可见的基础目录（用于 Docker 卷挂载源）。

        在 DooD 模式下，Docker 守护进程在宿主机运行，
        需要宿主机路径来创建绑定挂载。
        设置 DEER_FLOW_HOST_BASE_DIR 为容器 base_dir 对应的宿主机路径。

        未设置时回退到 base_dir（本地执行）。
        """
        if env := os.getenv("DEER_FLOW_HOST_BASE_DIR"):
            return Path(env)
        return self.base_dir

    def _host_base_dir_str(self) -> str:
        """返回宿主机基础目录的原始字符串（用于绑定挂载）。"""
        if env := os.getenv("DEER_FLOW_HOST_BASE_DIR"):
            return env
        return str(self.base_dir)

    @property
    def base_dir(self) -> Path:
        """所有应用数据的根目录。"""
        if self._base_dir is not None:
            return self._base_dir

        if env_home := os.getenv("DEER_FLOW_HOME"):
            return Path(env_home).resolve()

        return _default_local_base_dir()

    @property
    def memory_file(self) -> Path:
        """全局记忆文件路径: {base_dir}/memory.json"""
        return self.base_dir / "memory.json"

    @property
    def user_md_file(self) -> Path:
        """全局用户画像文件路径: {base_dir}/USER.md"""
        return self.base_dir / "USER.md"

    @property
    def agents_dir(self) -> Path:
        """旧布局共享代理目录: {base_dir}/agents/

        新代码应使用 user_agents_dir()。
        此属性仅作为未运行迁移脚本的安装的读取回退。
        """
        return self.base_dir / "agents"

    def agent_dir(self, name: str) -> Path:
        """旧布局按代理目录: {base_dir}/agents/{name}/"""
        return self.agents_dir / name.lower()

    def agent_memory_file(self, name: str) -> Path:
        """旧布局按代理记忆文件: {base_dir}/agents/{name}/memory.json"""
        return self.agent_dir(name) / "memory.json"

    def user_dir(self, user_id: str) -> Path:
        """按用户目录: {base_dir}/users/{user_id}/"""
        return self.base_dir / "users" / _validate_user_id(user_id)

    def user_memory_file(self, user_id: str) -> Path:
        """按用户记忆文件: {base_dir}/users/{user_id}/memory.json"""
        return self.user_dir(user_id) / "memory.json"

    def user_agents_dir(self, user_id: str) -> Path:
        """按用户代理根目录: {base_dir}/users/{user_id}/agents/"""
        return self.user_dir(user_id) / "agents"

    def user_agent_dir(self, user_id: str, agent_name: str) -> Path:
        """按用户按代理目录: {base_dir}/users/{user_id}/agents/{name}/"""
        return self.user_agents_dir(user_id) / agent_name.lower()

    def user_agent_memory_file(self, user_id: str, agent_name: str) -> Path:
        """按用户按代理记忆: {base_dir}/users/{user_id}/agents/{name}/memory.json"""
        return self.user_agent_dir(user_id, agent_name) / "memory.json"

    def thread_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """线程数据目录。

        有 user_id: {base_dir}/users/{user_id}/threads/{thread_id}/
        无 user_id（旧布局）: {base_dir}/threads/{thread_id}/

        包含 user-data/ 子目录，挂载为沙箱内的 /mnt/user-data/。
        """
        if user_id is not None:
            return self.user_dir(user_id) / "threads" / _validate_thread_id(thread_id)
        return self.base_dir / "threads" / _validate_thread_id(thread_id)

    def sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """沙箱工作空间目录（宿主机路径）。

        宿主机: {thread_dir}/user-data/workspace/
        沙箱内: /mnt/user-data/workspace/
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "workspace"

    def sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """用户上传文件目录（宿主机路径）。

        宿主机: {thread_dir}/user-data/uploads/
        沙箱内: /mnt/user-data/uploads/
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "uploads"

    def sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """Agent 生成物目录（宿主机路径）。

        宿主机: {thread_dir}/user-data/outputs/
        沙箱内: /mnt/user-data/outputs/
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "outputs"

    def acp_workspace_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """ACP 工作空间目录（宿主机路径）。

        宿主机: {thread_dir}/acp-workspace/
        沙箱内: /mnt/acp-workspace/

        每个线程独立的 ACP 工作空间，防止并发会话读取彼此的输出。
        """
        return self.thread_dir(thread_id, user_id=user_id) / "acp-workspace"

    def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """用户数据根目录（宿主机路径）。

        宿主机: {thread_dir}/user-data/
        沙箱内: /mnt/user-data/
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data"

    # ── host_* 方法：返回字符串，用于 Docker 卷挂载 ──

    def host_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """线程目录的宿主机路径（保留 Windows 路径格式）。"""
        if user_id is not None:
            return _join_host_path(self._host_base_dir_str(), "users", _validate_user_id(user_id), "threads", _validate_thread_id(thread_id))
        return _join_host_path(self._host_base_dir_str(), "threads", _validate_thread_id(thread_id))

    def host_sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """用户数据根目录的宿主机路径。"""
        return _join_host_path(self.host_thread_dir(thread_id, user_id=user_id), "user-data")

    def host_sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """工作空间目录的宿主机路径。"""
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=user_id), "workspace")

    def host_sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """上传目录的宿主机路径。"""
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=user_id), "uploads")

    def host_sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """输出目录的宿主机路径。"""
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=user_id), "outputs")

    def host_acp_workspace_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """ACP 工作空间的宿主机路径。"""
        return _join_host_path(self.host_thread_dir(thread_id, user_id=user_id), "acp-workspace")

    # ── 目录操作 ──

    def ensure_thread_dirs(self, thread_id: str, *, user_id: str | None = None) -> None:
        """创建线程的所有标准沙箱目录。

        权限设为 0o777，确保沙箱容器（可能以不同 UID 运行）能写入卷挂载路径。
        使用显式 chmod() 而非 mkdir(mode=...)，因为后者受进程 umask 影响可能不生效。

        包含 ACP 工作空间目录，即使首次 ACP 调用前也需要存在用于卷挂载。
        """
        for d in [
            self.sandbox_work_dir(thread_id, user_id=user_id),
            self.sandbox_uploads_dir(thread_id, user_id=user_id),
            self.sandbox_outputs_dir(thread_id, user_id=user_id),
            self.acp_workspace_dir(thread_id, user_id=user_id),
        ]:
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o777)

    def delete_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> None:
        """删除线程的所有持久化数据（幂等操作）。"""
        thread_dir = self.thread_dir(thread_id, user_id=user_id)
        if thread_dir.exists():
            shutil.rmtree(thread_dir)

    def resolve_virtual_path(self, thread_id: str, virtual_path: str, *, user_id: str | None = None) -> Path:
        """将沙箱虚拟路径解析为宿主机实际路径。

        Args:
            thread_id: 线程 ID
            virtual_path: 沙箱内看到的路径（如 /mnt/user-data/outputs/report.pdf）
            user_id: 可选的用户 ID

        Returns:
            解析后的绝对宿主机文件系统路径

        Raises:
            ValueError: 路径不以虚拟前缀开头，或检测到路径遍历

        安全：
            - 要求精确的段边界匹配（拒绝 /mnt/user-dataX/...）
            - 解析后检查 actual relative_to base，防止 ../../ 攻击
        """
        stripped = virtual_path.lstrip("/")
        prefix = VIRTUAL_PATH_PREFIX.lstrip("/")

        # 要求精确的段边界匹配，避免前缀混淆
        if stripped != prefix and not stripped.startswith(prefix + "/"):
            raise ValueError(f"Path must start with /{prefix}")

        relative = stripped[len(prefix) :].lstrip("/")
        base = self.sandbox_user_data_dir(thread_id, user_id=user_id).resolve()
        actual = (base / relative).resolve()

        # 路径遍历检测
        try:
            actual.relative_to(base)
        except ValueError:
            raise ValueError("Access denied: path traversal detected")

        return actual


# ── 全局单例 ──

_paths: Paths | None = None


def get_paths() -> Paths:
    """返回全局 Paths 单例（懒初始化）。"""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths


def resolve_path(path: str) -> Path:
    """将路径解析为绝对路径。

    相对路径基于应用基础目录解析。
    绝对路径原样返回（规范化后）。
    """
    p = Path(path)
    if not p.is_absolute():
        p = get_paths().base_dir / path
    return p.resolve()
