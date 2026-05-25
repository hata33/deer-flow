"""文件上传管理路由。

本模块实现了线程级别的文件上传、列表查询和删除功能。上传的文件
存储在线程专属的上传目录中，并可同步到沙箱环境供 AI 智能体访问。

核心功能：
- 多文件批量上传（支持大小和数量限制）
- 文件列表查询（含沙箱路径映射）
- 单文件删除（同时清理自动转换的 Markdown 副本）
- 上传限制查询

安全机制：
- 文件名规范化（normalize_filename）防止路径遍历
- 单文件大小限制（默认 50 MB）
- 总上传大小限制（默认 100 MB）
- 文件数量限制（默认 10 个）
- 符号链接检测与跳过
- 沙箱可写权限自动授予

附加功能：
- 可选的自动文档转换（Office/PDF → Markdown）
- 沙箱环境文件同步
- 重复文件名自动重命名

路由前缀：/api/threads/{thread_id}/uploads
标签：uploads
"""

import logging
import os
import stat

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.gateway.authz import require_permission
from app.gateway.deps import get_config
from deerflow.config.app_config import AppConfig
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.sandbox_provider import SandboxProvider, get_sandbox_provider
from deerflow.uploads.manager import (
    PathTraversalError,
    UnsafeUploadPathError,
    claim_unique_filename,
    delete_file_safe,
    enrich_file_listing,
    ensure_uploads_dir,
    get_uploads_dir,
    list_files_in_dir,
    normalize_filename,
    open_upload_file_no_symlink,
    upload_artifact_url,
    upload_virtual_path,
)
from deerflow.utils.file_conversion import CONVERTIBLE_EXTENSIONS, convert_file_to_markdown

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads/{thread_id}/uploads", tags=["uploads"])

UPLOAD_CHUNK_SIZE = 8192
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_MAX_TOTAL_SIZE = 100 * 1024 * 1024


class UploadResponse(BaseModel):
    """文件上传响应模型。

    Attributes:
        success: 上传是否全部成功（部分跳过时为 False）。
        files: 成功上传的文件信息列表。
        message: 结果描述消息。
        skipped_files: 因安全原因被跳过的文件列表。
    """

    success: bool
    files: list[dict[str, str]]
    message: str
    skipped_files: list[str] = Field(default_factory=list)


class UploadLimits(BaseModel):
    """应用级上传限制配置，供客户端查询。

    Attributes:
        max_files: 单次请求最大文件数。
        max_file_size: 单个文件最大字节数。
        max_total_size: 单次请求最大总字节数。
    """

    max_files: int
    max_file_size: int
    max_total_size: int


def _make_file_sandbox_writable(file_path: os.PathLike[str] | str) -> None:
    """确保上传文件在非本地沙箱中挂载后仍可写。

    在 AIO 沙箱模式下，网关先写入宿主机侧的权威文件，
    然后沙箱运行时可能重写同一挂载路径。此处授予 world-writable
    权限可防止网关用户和沙箱运行时用户之间的权限不匹配。

    Args:
        file_path: 文件路径。
    """
    file_stat = os.lstat(file_path)
    # 符号链接不修改权限，避免安全风险
    if stat.S_ISLNK(file_stat.st_mode):
        logger.warning("Skipping sandbox chmod for symlinked upload path: %s", file_path)
        return

    writable_mode = stat.S_IMODE(file_stat.st_mode) | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    chmod_kwargs = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
    os.chmod(file_path, writable_mode, **chmod_kwargs)


def _uses_thread_data_mounts(sandbox_provider: SandboxProvider) -> bool:
    """检查沙箱提供者是否使用线程数据直接挂载模式。

    直接挂载模式下无需手动同步文件到沙箱。

    Args:
        sandbox_provider: 沙箱提供者实例。

    Returns:
        True 如果使用线程数据挂载。
    """
    return bool(getattr(sandbox_provider, "uses_thread_data_mounts", False))


def _get_uploads_config_value(app_config: AppConfig, key: str, default: object) -> object:
    """从上传配置中读取值，同时支持字典和属性访问方式。

    Args:
        app_config: 应用配置对象。
        key: 配置键名。
        default: 默认值。

    Returns:
        配置值。
    """
    uploads_cfg = getattr(app_config, "uploads", None)
    if isinstance(uploads_cfg, dict):
        return uploads_cfg.get(key, default)
    return getattr(uploads_cfg, key, default)


def _get_upload_limit(app_config: AppConfig, key: str, default: int, *, legacy_key: str | None = None) -> int:
    """安全地读取上传限制值，支持遗留键名兼容。

    Args:
        app_config: 应用配置对象。
        key: 配置键名。
        default: 默认值。
        legacy_key: 遗留键名（可选）。

    Returns:
        上传限制整数值。
    """
    try:
        value = _get_uploads_config_value(app_config, key, None)
        if value is None and legacy_key is not None:
            value = _get_uploads_config_value(app_config, legacy_key, None)
        if value is None:
            value = default
        limit = int(value)
        if limit <= 0:
            raise ValueError
        return limit
    except Exception:
        logger.warning("Invalid uploads.%s value; falling back to %d", key, default)
        return default


def _get_upload_limits(app_config: AppConfig) -> UploadLimits:
    """获取当前的上传限制配置。

    Args:
        app_config: 应用配置对象。

    Returns:
        UploadLimits 实例。
    """
    return UploadLimits(
        max_files=_get_upload_limit(app_config, "max_files", DEFAULT_MAX_FILES, legacy_key="max_file_count"),
        max_file_size=_get_upload_limit(app_config, "max_file_size", DEFAULT_MAX_FILE_SIZE, legacy_key="max_single_file_size"),
        max_total_size=_get_upload_limit(app_config, "max_total_size", DEFAULT_MAX_TOTAL_SIZE),
    )


def _cleanup_uploaded_paths(paths: list[os.PathLike[str] | str]) -> None:
    """清理已写入的文件路径（在请求被拒绝时调用）。

    按逆序删除，避免目录层级问题。

    Args:
        paths: 需要清理的文件路径列表。
    """
    for path in reversed(paths):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Failed to clean up upload path after rejected request: %s", path, exc_info=True)


async def _write_upload_file_with_limits(
    file: UploadFile,
    *,
    uploads_dir: os.PathLike[str] | str,
    display_filename: str,
    max_single_file_size: int,
    max_total_size: int,
    total_size: int,
) -> tuple[os.PathLike[str] | str, int, int]:
    """将上传文件写入磁盘，同时实时检查单文件和总大小限制。

    采用流式写入方式，边读边写边检查，避免将整个文件加载到内存。
    超限或出错时自动清理已写入的部分文件。

    Args:
        file: FastAPI 上传文件对象。
        uploads_dir: 上传目录路径。
        display_filename: 显示用文件名。
        max_single_file_size: 单文件大小上限。
        max_total_size: 总大小上限。
        total_size: 当前累计总大小。

    Returns:
        (文件路径, 文件大小, 更新后的总大小) 元组。

    Raises:
        HTTPException: 状态码 413，当超过大小限制时抛出。
    """
    file_size = 0
    file_path, fh = open_upload_file_no_symlink(uploads_dir, display_filename)
    try:
        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            file_size += len(chunk)
            total_size += len(chunk)
            # 实时检查单文件大小限制
            if file_size > max_single_file_size:
                raise HTTPException(status_code=413, detail=f"File too large: {display_filename}")
            # 实时检查总上传大小限制
            if total_size > max_total_size:
                raise HTTPException(status_code=413, detail="Total upload size too large")
            fh.write(chunk)
    except Exception:
        fh.close()
        # 出错时清理部分写入的文件
        try:
            os.unlink(file_path)
        except FileNotFoundError:
            pass
        raise
    else:
        fh.close()
    return file_path, file_size, total_size


def _auto_convert_documents_enabled(app_config: AppConfig) -> bool:
    """检查是否启用自动宿主机侧文档转换功能。

    安全默认为禁用，除非运维人员在 config.yaml 中通过
    uploads.auto_convert_documents 显式启用。

    Args:
        app_config: 应用配置对象。

    Returns:
        True 如果启用了自动文档转换。
    """
    try:
        raw = _get_uploads_config_value(app_config, "auto_convert_documents", False)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    except Exception:
        return False


@router.post("", response_model=UploadResponse)
@require_permission("threads", "write", owner_check=True, require_existing=False)
async def upload_files(
    thread_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    config: AppConfig = Depends(get_config),
) -> UploadResponse:
    """批量上传文件到线程的上传目录。

    处理流程：
    1. 验证文件数量限制
    2. 创建上传目录
    3. 逐文件流式写入并检查大小限制
    4. 可选的自动文档转换
    5. 同步到沙箱环境（如需要）

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。
        files: 上传文件列表。
        config: 应用配置对象。

    Returns:
        UploadResponse，包含上传结果。

    Raises:
        HTTPException: 状态码 400（无文件/无效线程）、413（超限）。
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    limits = _get_upload_limits(config)
    if len(files) > limits.max_files:
        raise HTTPException(status_code=413, detail=f"Too many files: maximum is {limits.max_files}")

    try:
        uploads_dir = ensure_uploads_dir(thread_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    sandbox_uploads = get_paths().sandbox_uploads_dir(thread_id, user_id=get_effective_user_id())
    uploaded_files = []
    written_paths = []
    sandbox_sync_targets = []
    skipped_files = []
    total_size = 0
    # 跟踪本次请求中的文件名，防止重复表单字段静默截断。
    # 已有上传保持历史覆盖行为（单个替换上传）。
    seen_filenames: set[str] = set()

    sandbox_provider = get_sandbox_provider()
    # 判断是否需要手动同步文件到沙箱
    sync_to_sandbox = not _uses_thread_data_mounts(sandbox_provider)
    sandbox = None
    if sync_to_sandbox:
        sandbox_id = sandbox_provider.acquire(thread_id)
        sandbox = sandbox_provider.get(sandbox_id)
        if sandbox is None:
            raise HTTPException(status_code=500, detail="Failed to acquire sandbox")
    auto_convert_documents = _auto_convert_documents_enabled(config)

    for file in files:
        if not file.filename:
            continue

        try:
            original_filename = normalize_filename(file.filename)
            safe_filename = claim_unique_filename(original_filename, seen_filenames)
        except ValueError:
            logger.warning(f"Skipping file with unsafe filename: {file.filename!r}")
            continue

        try:
            file_path, file_size, total_size = await _write_upload_file_with_limits(
                file,
                uploads_dir=uploads_dir,
                display_filename=safe_filename,
                max_single_file_size=limits.max_file_size,
                max_total_size=limits.max_total_size,
                total_size=total_size,
            )
            written_paths.append(file_path)

            virtual_path = upload_virtual_path(safe_filename)

            if sync_to_sandbox:
                sandbox_sync_targets.append((file_path, virtual_path))

            file_info = {
                "filename": safe_filename,
                "size": str(file_size),
                "path": str(sandbox_uploads / safe_filename),
                "virtual_path": virtual_path,
                "artifact_url": upload_artifact_url(thread_id, safe_filename),
            }
            if safe_filename != original_filename:
                file_info["original_filename"] = original_filename

            logger.info(f"Saved file: {safe_filename} ({file_size} bytes) to {file_info['path']}")

            # 可选的自动文档转换（Office/PDF → Markdown）
            file_ext = file_path.suffix.lower()
            if auto_convert_documents and file_ext in CONVERTIBLE_EXTENSIONS:
                md_path = await convert_file_to_markdown(file_path)
                if md_path:
                    written_paths.append(md_path)
                    md_virtual_path = upload_virtual_path(md_path.name)

                    if sync_to_sandbox:
                        sandbox_sync_targets.append((md_path, md_virtual_path))

                    file_info["markdown_file"] = md_path.name
                    file_info["markdown_path"] = str(sandbox_uploads / md_path.name)
                    file_info["markdown_virtual_path"] = md_virtual_path
                    file_info["markdown_artifact_url"] = upload_artifact_url(thread_id, md_path.name)

            uploaded_files.append(file_info)

        except HTTPException as e:
            # 上传失败时清理已写入的文件
            _cleanup_uploaded_paths(written_paths)
            raise e
        except UnsafeUploadPathError as e:
            # 不安全的目标路径，跳过该文件继续处理其他文件
            logger.warning("Skipping upload with unsafe destination %s: %s", file.filename, e)
            skipped_files.append(safe_filename)
            continue
        except Exception as e:
            logger.error(f"Failed to upload {file.filename}: {e}")
            _cleanup_uploaded_paths(written_paths)
            raise HTTPException(status_code=500, detail=f"Failed to upload {file.filename}: {str(e)}")

    # 手动同步文件到沙箱环境
    if sync_to_sandbox:
        for file_path, virtual_path in sandbox_sync_targets:
            _make_file_sandbox_writable(file_path)
            sandbox.update_file(virtual_path, file_path.read_bytes())

    message = f"Successfully uploaded {len(uploaded_files)} file(s)"
    if skipped_files:
        message += f"; skipped {len(skipped_files)} unsafe file(s)"

    return UploadResponse(
        success=not skipped_files,
        files=uploaded_files,
        message=message,
        skipped_files=skipped_files,
    )


@router.get("/limits", response_model=UploadLimits)
@require_permission("threads", "read", owner_check=True)
async def get_upload_limits(
    thread_id: str,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> UploadLimits:
    """查询当前线程的上传限制配置。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。
        config: 应用配置对象。

    Returns:
        UploadLimits，包含各项上传限制值。
    """
    return _get_upload_limits(config)


@router.get("/list", response_model=dict)
@require_permission("threads", "read", owner_check=True)
async def list_uploaded_files(thread_id: str, request: Request) -> dict:
    """列出线程上传目录中的所有文件。

    返回文件列表，包含文件名、大小、路径等信息，
    并额外附加沙箱相对路径。

    Args:
        thread_id: 线程 ID。
        request: FastAPI 请求对象。

    Returns:
        包含文件列表的字典。

    Raises:
        HTTPException: 状态码 400，当线程 ID 无效时抛出。
    """
    try:
        uploads_dir = get_uploads_dir(thread_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = list_files_in_dir(uploads_dir)
    enrich_file_listing(result, thread_id)

    # 网关额外附加沙箱相对路径
    sandbox_uploads = get_paths().sandbox_uploads_dir(thread_id, user_id=get_effective_user_id())
    for f in result["files"]:
        f["path"] = str(sandbox_uploads / f["filename"])

    return result


@router.delete("/{filename}")
@require_permission("threads", "delete", owner_check=True, require_existing=True)
async def delete_uploaded_file(thread_id: str, filename: str, request: Request) -> dict:
    """从线程的上传目录中删除指定文件。

    同时清理自动转换生成的 Markdown 副本文件。

    Args:
        thread_id: 线程 ID。
        filename: 文件名。
        request: FastAPI 请求对象。

    Returns:
        包含删除结果的字典。

    Raises:
        HTTPException: 状态码 400（路径无效）、404（文件不存在）。
    """
    try:
        uploads_dir = get_uploads_dir(thread_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        return delete_file_safe(uploads_dir, filename, convertible_extensions=CONVERTIBLE_EXTENSIONS)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    except PathTraversalError:
        raise HTTPException(status_code=400, detail="Invalid path")
    except Exception as e:
        logger.error(f"Failed to delete {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete {filename}: {str(e)}")
