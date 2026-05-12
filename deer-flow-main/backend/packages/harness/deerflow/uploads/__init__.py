"""文件上传管理模块。

提供线程隔离的文件上传、列表、删除等操作，包含路径遍历防护。
纯业务逻辑——无 FastAPI/HTTP 依赖，Gateway 和 Client 共用。
"""

from .manager import (
    PathTraversalError,
    claim_unique_filename,
    delete_file_safe,
    enrich_file_listing,
    ensure_uploads_dir,
    get_uploads_dir,
    list_files_in_dir,
    normalize_filename,
    upload_artifact_url,
    upload_virtual_path,
    validate_path_traversal,
    validate_thread_id,
)

__all__ = [
    "get_uploads_dir",         # 获取线程上传目录路径（无副作用）
    "ensure_uploads_dir",      # 确保线程上传目录存在（按需创建）
    "normalize_filename",      # 清理文件名（去除目录组件，拒绝遍历模式）
    "PathTraversalError",      # 路径遍历异常
    "claim_unique_filename",   # 生成不重复的文件名（冲突时追加 _N 后缀）
    "validate_path_traversal", # 验证路径不越界
    "list_files_in_dir",       # 列出目录中的文件
    "delete_file_safe",        # 安全删除文件（含路径遍历检查）
    "upload_artifact_url",     # 构建产物访问 URL
    "upload_virtual_path",     # 构建虚拟路径
    "enrich_file_listing",     # 为文件列表添加虚拟路径和 URL
    "validate_thread_id",      # 校验线程 ID 安全性
]
