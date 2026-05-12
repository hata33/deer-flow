"""文件上传管理核心逻辑。

纯业务逻辑——无 FastAPI/HTTP 依赖。
Gateway 和 Client 共用此模块进行文件上传、列表、删除等操作。

安全防护：
- thread_id 校验：仅允许字母、数字、连字符、下划线、点号
- 文件名规范化：去除目录组件，拒绝遍历模式（..）
- 路径遍历校验：确保所有文件操作在允许的基础目录内
"""

import os
import re
from pathlib import Path
from urllib.parse import quote

from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths


class PathTraversalError(ValueError):
    """路径遍历攻击异常，当路径逃出允许的基础目录时抛出。"""


# thread_id 安全校验：仅允许字母、数字、连字符、下划线、点号
_SAFE_THREAD_ID = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_thread_id(thread_id: str) -> None:
    """校验线程 ID 的安全性，拒绝包含文件系统不安全字符的 ID。

    Raises:
        ValueError: 线程 ID 为空或包含不安全字符。
    """
    if not thread_id or not _SAFE_THREAD_ID.match(thread_id):
        raise ValueError(f"Invalid thread_id: {thread_id!r}")


def get_uploads_dir(thread_id: str) -> Path:
    """获取线程上传目录路径（无副作用，不创建目录）。

    Args:
        thread_id: 线程 ID。

    Returns:
        上传目录的绝对路径。
    """
    validate_thread_id(thread_id)
    return get_paths().sandbox_uploads_dir(thread_id)


def ensure_uploads_dir(thread_id: str) -> Path:
    """确保线程上传目录存在，不存在时递归创建。

    Args:
        thread_id: 线程 ID。

    Returns:
        上传目录的绝对路径。
    """
    base = get_uploads_dir(thread_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


def normalize_filename(filename: str) -> str:
    """规范化文件名，提取纯文件名并拒绝不安全模式。

    去除目录组件（Path.name），拒绝空名、点号遍历（. 和 ..）、
    反斜杠（Windows 风格路径注入），以及超过 255 字节的文件名。

    Args:
        filename: 用户输入的原始文件名（可能包含路径组件）。

    Returns:
        安全的纯文件名。

    Raises:
        ValueError: 文件名为空、不安全或过长。
    """
    if not filename:
        raise ValueError("Filename is empty")
    safe = Path(filename).name
    if not safe or safe in {".", ".."}:
        raise ValueError(f"Filename is unsafe: {filename!r}")
    # 拒绝反斜杠——Linux 上 Path.name 保留为字面字符，
    # 但反斜杠暗示 Windows 风格路径注入
    if "\\" in safe:
        raise ValueError(f"Filename contains backslash: {filename!r}")
    if len(safe.encode("utf-8")) > 255:
        raise ValueError(f"Filename too long: {len(safe)} chars")
    return safe


def claim_unique_filename(name: str, seen: set[str]) -> str:
    """生成不重复的文件名，冲突时追加 _N 后缀。

    自动将返回的文件名加入 seen 集合，调用方无需额外处理。

    Args:
        name: 候选文件名。
        seen: 已占用的文件名集合（会被原地修改）。

    Returns:
        不与 seen 冲突的文件名（已加入 seen）。
    """
    if name not in seen:
        seen.add(name)
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    candidate = f"{stem}_{counter}{suffix}"
    while candidate in seen:
        counter += 1
        candidate = f"{stem}_{counter}{suffix}"
    seen.add(candidate)
    return candidate


def validate_path_traversal(path: Path, base: Path) -> None:
    """验证路径位于基础目录内，防止路径遍历攻击。

    Args:
        path: 待验证的路径。
        base: 允许的基础目录。

    Raises:
        PathTraversalError: 路径逃出基础目录。
    """
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise PathTraversalError("Path traversal detected") from None


def list_files_in_dir(directory: Path) -> dict:
    """列出目录中的所有文件（不含子目录），按名称排序。

    Args:
        directory: 待扫描的目录。

    Returns:
        包含 "files" 列表和 "count" 的字典。
        每个文件条目包含 filename、size（int 字节）、path、extension、modified。
    """
    if not directory.is_dir():
        return {"files": [], "count": 0}

    files = []
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            files.append(
                {
                    "filename": entry.name,
                    "size": st.st_size,
                    "path": entry.path,
                    "extension": Path(entry.name).suffix,
                    "modified": st.st_mtime,
                }
            )
    return {"files": files, "count": len(files)}


def delete_file_safe(base_dir: Path, filename: str, *, convertible_extensions: set[str] | None = None) -> dict:
    """安全删除文件，包含路径遍历验证和伴随文件清理。

    删除前验证路径不越界。若提供了 convertible_extensions 且文件扩展名匹配，
    同时删除转换时生成的伴随 .md 文件。

    Args:
        base_dir: 文件所在的基础目录。
        filename: 待删除的文件名。
        convertible_extensions: 需要清理伴随 .md 文件的扩展名集合。

    Returns:
        包含 success 和 message 的字典。

    Raises:
        FileNotFoundError: 文件不存在。
        PathTraversalError: 路径遍历检测。
    """
    file_path = (base_dir / filename).resolve()
    validate_path_traversal(file_path, base_dir)

    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {filename}")

    file_path.unlink()

    # 清理上传转换时生成的伴随 Markdown 文件
    if convertible_extensions and file_path.suffix.lower() in convertible_extensions:
        file_path.with_suffix(".md").unlink(missing_ok=True)

    return {"success": True, "message": f"Deleted {filename}"}


def upload_artifact_url(thread_id: str, filename: str) -> str:
    """构建上传文件的产物访问 URL，文件名经 percent 编码。

    Args:
        thread_id: 线程 ID。
        filename: 文件名。

    Returns:
        产物 URL 路径。
    """
    return f"/api/threads/{thread_id}/artifacts{VIRTUAL_PATH_PREFIX}/uploads/{quote(filename, safe='')}"


def upload_virtual_path(filename: str) -> str:
    """构建上传文件的虚拟路径。

    Args:
        filename: 文件名。

    Returns:
        虚拟路径字符串。
    """
    return f"{VIRTUAL_PATH_PREFIX}/uploads/{filename}"


def enrich_file_listing(result: dict, thread_id: str) -> dict:
    """为文件列表结果添加虚拟路径、产物 URL，并将 size 转为字符串。

    原地修改 result 并返回。

    Args:
        result: list_files_in_dir 的返回值。
        thread_id: 线程 ID。

    Returns:
        丰富后的文件列表字典。
    """
    for f in result["files"]:
        filename = f["filename"]
        f["size"] = str(f["size"])
        f["virtual_path"] = upload_virtual_path(filename)
        f["artifact_url"] = upload_artifact_url(thread_id, filename)
    return result
