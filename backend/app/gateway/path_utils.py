"""线程虚拟路径解析工具 — 沙箱路径到宿主机路径的转换。

本模块提供了将沙箱内的虚拟路径（如 /mnt/user-data/outputs/...）
解析为宿主机实际文件系统路径的功能。

核心职责：
  - 将 Agent 在沙箱中看到的虚拟路径转换为宿主机物理路径
  - 路径遍历攻击检测：拒绝包含 ".." 的恶意路径
  - 用户隔离：路径解析时注入当前有效用户 ID，确保跨用户隔离

使用场景：
  - Artifacts 路由：提供 /api/threads/{id}/artifacts/{path} 端点
    下载沙箱内生成的文件
  - Uploads 路由：管理沙箱内上传文件目录

设计要点：
  - 虚拟路径遵循 /mnt/user-data/{workspace,uploads,outputs} 约定
  - 实际路径存储在 {base_dir}/users/{user_id}/threads/{thread_id}/user-data/ 下
  - 路径遍历尝试返回 403（而非 400），以便安全审计区分
"""

from pathlib import Path

from fastapi import HTTPException

from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id


def resolve_thread_virtual_path(thread_id: str, virtual_path: str) -> Path:
    """将虚拟路径解析为线程用户数据目录下的实际文件系统路径。

    Args:
        thread_id: 线程 ID。
        virtual_path: 沙箱内看到的虚拟路径
                      （如 /mnt/user-data/outputs/file.txt）。

    Returns:
        解析后的文件系统路径。

    Raises:
        HTTPException 403: 路径包含遍历攻击（".."）。
        HTTPException 400: 路径格式无效。
    """
    try:
        return get_paths().resolve_virtual_path(thread_id, virtual_path, user_id=get_effective_user_id())
    except ValueError as e:
        # 路径遍历尝试返回 403（安全事件），其他格式错误返回 400
        status = 403 if "traversal" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e))
