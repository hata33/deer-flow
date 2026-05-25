"""文件上传（Uploads）模块 —— 上传管理公共接口。

本模块封装了 DeerFlow 系统中文件上传相关的核心业务逻辑，提供从文件名
规范化、路径安全校验到文件读写和虚拟路径构造的完整工具链。

架构定位：
    本模块是纯业务逻辑层，**不依赖 FastAPI 或任何 HTTP 框架**。
    Gateway（API 层）和 Client（客户端层）均委托调用此处的函数，
    确保安全策略在所有入口处一致执行。

核心安全机制：
    - **路径遍历防护** —— ``validate_path_traversal`` 确保文件操作
      不超出允许的基础目录。
    - **符号链接安全写入** —— ``open_upload_file_no_symlink`` 在 POSIX
      上使用 ``O_NOFOLLOW`` 标志，在 Windows 上使用双重 ``lstat`` 检查，
      防止沙箱进程通过符号链接越权写入。
    - **文件名规范化** —— ``normalize_filename`` 剥离目录组件、
      拒绝穿越模式、限制字节长度。
    - **线程 ID 校验** —— ``validate_thread_id`` 确保线程标识符
      仅包含文件系统安全的字符。

模块导出：
    - :func:`get_uploads_dir` —— 获取线程的上传目录路径（无副作用）
    - :func:`ensure_uploads_dir` —— 获取并创建上传目录
    - :func:`normalize_filename` —— 文件名安全规范化
    - :func:`claim_unique_filename` —— 重名文件自动追加序号
    - :func:`validate_path_traversal` —— 路径遍历校验
    - :func:`validate_thread_id` —— 线程 ID 格式校验
    - :func:`list_files_in_dir` —— 列出目录中的文件
    - :func:`delete_file_safe` —— 安全删除文件（含路径校验）
    - :func:`upload_artifact_url` —— 构建文件的 artifact URL
    - :func:`upload_virtual_path` —— 构建文件的虚拟路径
    - :func:`enrich_file_listing` —— 为文件列表添加虚拟路径和 URL
    - :class:`PathTraversalError` —— 路径遍历异常
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
    "get_uploads_dir",
    "ensure_uploads_dir",
    "normalize_filename",
    "PathTraversalError",
    "claim_unique_filename",
    "validate_path_traversal",
    "list_files_in_dir",
    "delete_file_safe",
    "upload_artifact_url",
    "upload_virtual_path",
    "enrich_file_listing",
    "validate_thread_id",
]
