"""上传管理器 —— 文件上传核心业务逻辑。

本模块实现了 DeerFlow 系统中文件上传的全部安全策略和文件操作逻辑。
作为纯业务逻辑层（无 FastAPI/HTTP 依赖），Gateway 和 Client 均委托
调用此处的函数，确保安全策略在所有入口处一致执行。

核心安全机制：
    1. **路径遍历防护** —— ``validate_path_traversal`` 通过
       ``Path.resolve()`` + ``relative_to()`` 双重检查，确保文件操作
       不超出允许的基础目录（``base_dir``）。

    2. **符号链接安全写入** —— ``open_upload_file_no_symlink`` 是本模块
       最关键的安全函数。由于上传目录可能被挂载到本地沙箱中，沙箱进程
       有机会在未来的上传文件名处创建符号链接。如果使用普通的
       ``Path.write_bytes`` 写入，会跟随符号链接，导致以 Gateway 权限
       覆盖沙箱外的文件。本函数的防护策略：
       - **POSIX**：使用 ``O_NOFOLLOW`` 标志调用 ``os.open()``，
         当目标是符号链接时返回 ``ELOOP`` 错误，从内核层面阻止攻击。
       - **Windows**：不支持 ``O_NOFOLLOW``，采用双重 ``lstat`` +
         ``fstat`` 检查缩小 TOCTOU 竞态窗口（无法完全消除，但显著
         提高攻击难度），配合路径遍历检查作为纵深防御。

    3. **文件名规范化** —— ``normalize_filename`` 剥离目录组件、
       拒绝 ``..`` 穿越、拒绝反斜杠（防止 Windows 路径注入）、
       限制 UTF-8 字节长度不超过 255（符合文件系统限制）。

    4. **线程 ID 校验** —— ``validate_thread_id`` 确保线程标识符
       仅包含 ``[a-zA-Z0-9._-]``，防止路径注入。

文件操作功能：
    - ``write_upload_file_no_symlink`` —— 安全写入上传文件
    - ``list_files_in_dir`` —— 列出目录中的文件（不跟随符号链接）
    - ``delete_file_safe`` —— 安全删除文件（含路径校验和伴侣文件清理）
    - ``enrich_file_listing`` —— 为文件列表添加虚拟路径和 artifact URL

虚拟路径体系：
    DeerFlow 使用虚拟路径前缀（``VIRTUAL_PATH_PREFIX``）为上传文件
    构建统一的访问路径，支持通过 API 的 artifact 端点进行文件下载。
"""

import errno
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote

from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id


class PathTraversalError(ValueError):
    """路径遍历异常 —— 当文件操作路径逃逸出允许的基础目录时抛出。

    这是一个安全异常，表示检测到可能的路径遍历攻击。
    继承自 ``ValueError``，因为本质上是一个非法的参数值。
    """


class UnsafeUploadPathError(ValueError):
    """不安全上传路径异常 —— 当上传目标路径不符合安全要求时抛出。

    可能的原因包括：目标是符号链接、目录、硬链接、或非常规文件。
    """


# 线程 ID 安全校验正则：仅允许字母、数字、点、连字符和下划线。
# 排除斜杠、反斜杠、空字节等可能在文件系统路径中被特殊解释的字符。
_SAFE_THREAD_ID = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_thread_id(thread_id: str) -> None:
    """校验线程 ID 是否仅包含文件系统安全的字符。

    线程 ID 会被直接用作上传目录路径的组成部分，因此必须排除
    任何可能在文件系统中被特殊解释的字符（如 ``/``, ``\\``, ``..`` 等）。

    Args:
        thread_id: 待校验的线程标识符字符串。

    Raises:
        ValueError: 线程 ID 为空或包含非安全字符时抛出。
    """
    if not thread_id or not _SAFE_THREAD_ID.match(thread_id):
        raise ValueError(f"Invalid thread_id: {thread_id!r}")


def get_uploads_dir(thread_id: str) -> Path:
    """获取指定线程的上传目录路径（纯查询，无副作用）。

    仅计算路径并返回，不会创建目录。适用于路径计算和存在性检查。

    Args:
        thread_id: 线程标识符，需通过 ``validate_thread_id`` 校验。

    Returns:
        该线程上传目录的 ``Path`` 对象。

    Raises:
        ValueError: 线程 ID 格式不合法。
    """
    validate_thread_id(thread_id)
    return get_paths().sandbox_uploads_dir(thread_id, user_id=get_effective_user_id())


def ensure_uploads_dir(thread_id: str) -> Path:
    """获取并确保创建指定线程的上传目录。

    如果目录不存在，会递归创建所有父目录（等同于 ``mkdir -p``）。

    Args:
        thread_id: 线程标识符。

    Returns:
        该线程上传目录的 ``Path`` 对象。

    Raises:
        ValueError: 线程 ID 格式不合法。
    """
    base = get_uploads_dir(thread_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


def normalize_filename(filename: str) -> str:
    """规范化文件名，剥离不安全成分。

    处理用户提交的原始文件名，执行以下安全措施：
    1. 使用 ``Path.name`` 剥离所有目录组件（如 ``../../etc/passwd`` → ``passwd``）。
    2. 拒绝空文件名和穿越模式（``.``, ``..``）。
    3. 拒绝包含反斜杠的文件名 —— 在 Linux 上 ``Path.name`` 会将反斜杠
       视为字面字符保留，但这通常意味着 Windows 风格路径注入尝试。
    4. 限制文件名 UTF-8 编码字节长度不超过 255（ext4 等文件系统的限制）。

    Args:
        filename: 用户提交的原始文件名（可能包含路径组件）。

    Returns:
        安全的纯文件名（仅 basename 部分）。

    Raises:
        ValueError: 文件名为空、为穿越模式、包含反斜杠、或字节长度超限。
    """
    if not filename:
        raise ValueError("Filename is empty")
    # Path.name 自动剥离目录前缀，只保留最后的文件名部分
    safe = Path(filename).name
    if not safe or safe in {".", ".."}:
        raise ValueError(f"Filename is unsafe: {filename!r}")
    # 在 Linux 上 Path.name 不会将反斜杠视为路径分隔符，
    # 但反斜杠出现通常意味着 Windows 路径注入，需要拒绝
    if "\\" in safe:
        raise ValueError(f"Filename contains backslash: {filename!r}")
    # 文件系统对文件名有字节长度限制（ext4 为 255 字节），
    # 使用 UTF-8 编码后的字节长度进行校验
    if len(safe.encode("utf-8")) > 255:
        raise ValueError(f"Filename too long: {len(safe)} chars")
    return safe


def claim_unique_filename(name: str, seen: set[str]) -> str:
    """在重名时自动追加序号，生成唯一文件名。

    当同一批次上传中存在同名文件时，自动在文件名主干后追加 ``_N``
    序号（如 ``report.pdf`` → ``report_1.pdf`` → ``report_2.pdf``）。
    返回的文件名会自动加入 ``seen`` 集合，调用者无需手动维护。

    Args:
        name: 候选文件名。
        seen: 已使用的文件名集合（会被原地修改）。

    Returns:
        不在 ``seen`` 中的唯一文件名（已加入 ``seen``）。
    """
    if name not in seen:
        seen.add(name)
        return name
    # 分离文件名主干和扩展名，用于追加序号
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    candidate = f"{stem}_{counter}{suffix}"
    # 线性搜索第一个可用的序号，通常冲突很少，性能可接受
    while candidate in seen:
        counter += 1
        candidate = f"{stem}_{counter}{suffix}"
    seen.add(candidate)
    return candidate


def validate_path_traversal(path: Path, base: Path) -> None:
    """校验路径是否在允许的基础目录内。

    通过 ``resolve()`` 解析所有符号链接和 ``..`` 组件后，
    检查结果路径是否是基础目录的子路径。

    Args:
        path: 待校验的目标路径。
        base: 允许的基础目录。

    Raises:
        PathTraversalError: 路径逃逸出基础目录时抛出。
    """
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise PathTraversalError("Path traversal detected") from None


def open_upload_file_no_symlink(base_dir: Path, filename: str) -> tuple[Path, object]:
    """安全地打开上传目标文件用于流式写入。

    这是本模块最关键的安全函数。由于上传目录可能被挂载到本地沙箱中，
    沙箱进程有机会在未来的上传文件名处创建符号链接。如果使用普通的
    ``open()`` 或 ``Path.write_bytes`` 写入，会跟随符号链接，导致以
    Gateway 权限覆盖沙箱外的任意文件。

    防护策略按操作系统分为两套：
    - **POSIX（Linux/macOS）**：使用 ``O_NOFOLLOW`` 标志调用 ``os.open()``，
      内核在目标为符号链接时直接返回 ``ELOOP`` 错误，从内核层面阻断攻击。
      额外检查 ``st_nlink == 1`` 确保文件不是硬链接。
    - **Windows**：不支持 ``O_NOFOLLOW``，采用双重 ``lstat``（open 前后各一次）
      + ``fstat`` 验证，缩小 TOCTOU 竞态窗口。配合路径遍历检查作为纵深防御。
      注意：Windows 上存在理论上的竞态条件（攻击者在 lstat 和 open 之间替换文件），
      但路径遍历检查限制了攻击者能到达的目标范围。

    Args:
        base_dir: 上传文件所在的基础目录（安全边界）。
        filename: 用户提交的原始文件名（会先经过 ``normalize_filename`` 规范化）。

    Returns:
        二元组 ``(dest_path, file_handle)``：
        - ``dest_path``：写入目标的 ``Path`` 对象。
        - ``file_handle``：已打开的二进制写文件句柄（``"wb"`` 模式）。
        调用者负责关闭文件句柄。

    Raises:
        UnsafeUploadPathError: 目标路径不安全（符号链接、目录、硬链接等）。
        PathTraversalError: 路径逃逸出基础目录。
        ValueError: 文件名不合法。
    """
    safe_name = normalize_filename(filename)
    dest = base_dir / safe_name

    # 第一次 lstat：检查目标路径当前的状态（不跟随符号链接）
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None

    # 如果目标存在但不是常规文件（可能是符号链接、目录、设备文件等），直接拒绝
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise UnsafeUploadPathError(f"Upload destination is not a regular file: {safe_name}")

    # 路径遍历校验：确保规范化后的路径仍在基础目录内
    validate_path_traversal(dest, base_dir)

    # 检测当前平台是否支持 O_NOFOLLOW（POSIX 特有标志）
    has_nofollow = hasattr(os, "O_NOFOLLOW")

    if has_nofollow:
        # ===================== POSIX 安全写入路径 =====================
        # O_NOFOLLOW 使 open() 在目标为符号链接时返回 ELOOP，
        # 从操作系统内核层面阻止符号链接攻击
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
        # O_NONBLOCK 防止在特殊文件（如 FIFO）上阻塞
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK

        try:
            fd = os.open(dest, flags, 0o600)
        except OSError as exc:
            # ELOOP: 符号链接; EISDIR: 目录; ENOTDIR: 路径组件非目录;
            # ENXIO: 特殊文件; EAGAIN: 不可访问
            if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO, errno.EAGAIN}:
                raise UnsafeUploadPathError(f"Unsafe upload destination: {safe_name}") from exc
            raise

        try:
            # fstat 使用已打开的文件描述符，确保检查的是实际打开的文件
            opened_stat = os.fstat(fd)
            # st_nlink == 1 确保文件没有硬链接到其他位置
            if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
                raise UnsafeUploadPathError(f"Upload destination is not an exclusive regular file: {safe_name}")
            # 清空文件内容（如果文件已存在），确保从零开始写入
            os.ftruncate(fd, 0)
            fh = os.fdopen(fd, "wb")
            fd = -1  # 标记 fd 已被 fdopen 接管，避免 finally 中重复关闭
        finally:
            if fd >= 0:
                os.close(fd)
        return dest, fh

    # ===================== Windows 安全写入路径 =====================
    # Windows 不支持 O_NOFOLLOW，采用缩小 TOCTOU 窗口的防御策略：
    # 1. open 前后各做一次 lstat 检查
    # 2. open 后做 fstat 验证
    # 3. 路径遍历检查限制攻击范围
    # 注意：存在理论上的竞态条件，但实际利用难度很高
    if st is not None and st.st_nlink > 1:
        raise UnsafeUploadPathError(f"Upload destination has multiple links: {safe_name}")

    flags = os.O_WRONLY | os.O_CREAT
    # Windows 需要 O_BINARY 标志以避免换行符自动转换（\n → \r\n）
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    # 第二次 lstat：在 open 之前立即检查，缩小 TOCTOU 竞态窗口
    try:
        pre_open_st = os.lstat(dest)
    except FileNotFoundError:
        pre_open_st = None

    # open 前的最终校验
    if pre_open_st is not None and not stat.S_ISREG(pre_open_st.st_mode):
        raise UnsafeUploadPathError(f"Upload destination is not a regular file: {safe_name}")
    if pre_open_st is not None and pre_open_st.st_nlink > 1:
        raise UnsafeUploadPathError(f"Upload destination has multiple links: {safe_name}")

    try:
        fd = os.open(dest, flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.EISDIR, errno.ENOTDIR, errno.ENXIO, errno.EAGAIN}:
            raise UnsafeUploadPathError(f"Unsafe upload destination: {safe_name}") from exc
        raise

    try:
        # fstat 验证：检查通过 fd 实际打开的文件是否为常规文件
        opened_stat = os.fstat(fd)
        # Windows 上允许 st_nlink > 1（NTFS 硬链接更常见），但仍检查为常规文件
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink > 1:
            raise UnsafeUploadPathError(f"Upload destination is not an exclusive regular file: {safe_name}")
        os.ftruncate(fd, 0)
        fh = os.fdopen(fd, "wb")
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
    return dest, fh


def write_upload_file_no_symlink(base_dir: Path, filename: str, data: bytes) -> Path:
    """安全写入上传文件，防止符号链接攻击。

    封装 ``open_upload_file_no_symlink``，提供一次性写入完整文件内容的
    便捷接口。文件句柄会在写入完成后自动关闭。

    Args:
        base_dir: 上传文件所在的基础目录。
        filename: 用户提交的原始文件名。
        data: 要写入的文件内容（字节数据）。

    Returns:
        写入目标的 ``Path`` 对象。

    Raises:
        UnsafeUploadPathError: 目标路径不安全。
        PathTraversalError: 路径逃逸。
    """
    dest, fh = open_upload_file_no_symlink(base_dir, filename)
    with fh:
        fh.write(data)
    return dest


def list_files_in_dir(directory: Path) -> dict:
    """列出目录中的文件（不含子目录），不跟随符号链接。

    使用 ``os.scandir`` 进行高效的目录遍历，通过
    ``follow_symlinks=False`` 确保符号链接不被跟随（防止信息泄露）。

    Args:
        directory: 待扫描的目录路径。

    Returns:
        包含 ``"files"`` 列表和 ``"count"`` 的字典。
        每个文件条目包含以下字段：
        - ``filename`` (str): 文件名
        - ``size`` (int): 文件大小（字节）
        - ``path`` (str): 文件完整路径
        - ``extension`` (str): 文件扩展名（含点号）
        - ``modified`` (float): 最后修改时间（Unix 时间戳）
        文件按名称排序。目录不存在时返回空列表。

    Note:
        返回的 ``size`` 为整数。如需人类可读的字符串格式，
        请调用 ``enrich_file_listing`` 进行后处理。
    """
    if not directory.is_dir():
        return {"files": [], "count": 0}

    files = []
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            # follow_symlinks=False：不跟随符号链接，只列出真实的文件
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
    """安全删除基础目录内的文件（含路径遍历校验）。

    删除前会进行路径遍历校验，确保不会越权删除基础目录外的文件。
    当文件属于可转换类型（如 PDF、Word）时，会同时清理转换生成的
    伴侣 Markdown 文件（同目录下同名的 ``.md`` 文件）。

    Args:
        base_dir: 文件所在的基础目录（安全边界）。
        filename: 待删除的文件名。
        convertible_extensions: 需要清理伴侣 Markdown 的文件扩展名集合
            （如 ``{".pdf", ".docx"}``）。为 ``None`` 时不清理伴侣文件。

    Returns:
        包含 ``success`` (bool) 和 ``message`` (str) 的字典。

    Raises:
        FileNotFoundError: 指定文件不存在。
        PathTraversalError: 文件路径逃逸出基础目录。
    """
    file_path = (base_dir / filename).resolve()
    validate_path_traversal(file_path, base_dir)

    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {filename}")

    file_path.unlink()

    # 清理上传时转换生成的伴侣 Markdown 文件。
    # 例如删除 ``report.pdf`` 时，同时删除 ``report.md``。
    if convertible_extensions and file_path.suffix.lower() in convertible_extensions:
        file_path.with_suffix(".md").unlink(missing_ok=True)

    return {"success": True, "message": f"Deleted {filename}"}


def upload_artifact_url(thread_id: str, filename: str) -> str:
    """构建上传文件的 artifact API URL。

    生成通过 DeerFlow REST API 的 artifact 端点访问文件的完整 URL 路径。
    文件名会进行百分号编码（percent-encoding），确保空格、``#``、``?``
    等特殊字符在 URL 中安全传输。

    Args:
        thread_id: 线程标识符。
        filename: 文件名。

    Returns:
        artifact URL 路径字符串，如：
        ``"/api/threads/{thread_id}/artifacts/virtual/uploads/report.pdf"``
    """
    return f"/api/threads/{thread_id}/artifacts{VIRTUAL_PATH_PREFIX}/uploads/{quote(filename, safe='')}"


def upload_virtual_path(filename: str) -> str:
    """构建上传文件的虚拟路径。

    生成 DeerFlow 虚拟文件系统中的路径标识符。虚拟路径不对应磁盘上的
    实际位置，而是通过 API 端点映射到实际文件。

    Args:
        filename: 文件名。

    Returns:
        虚拟路径字符串，如 ``"/virtual/uploads/report.pdf"``。
    """
    return f"{VIRTUAL_PATH_PREFIX}/uploads/{filename}"


def enrich_file_listing(result: dict, thread_id: str) -> dict:
    """为文件列表结果添加虚拟路径、artifact URL 和格式化大小。

    对 ``list_files_in_dir`` 返回的结果进行后处理：
    1. 将 ``size`` 从整数转为字符串（兼容 JSON 序列化精度要求）。
    2. 添加 ``virtual_path`` 字段（虚拟文件系统路径）。
    3. 添加 ``artifact_url`` 字段（API 访问 URL）。

    直接修改 ``result`` 字典（in-place），同时返回该字典以支持链式调用。

    Args:
        result: ``list_files_in_dir`` 返回的文件列表字典。
        thread_id: 线程标识符（用于构建 artifact URL）。

    Returns:
        经过丰富化处理的同一 ``result`` 字典。
    """
    for f in result["files"]:
        filename = f["filename"]
        f["size"] = str(f["size"])
        f["virtual_path"] = upload_virtual_path(filename)
        f["artifact_url"] = upload_artifact_url(thread_id, filename)
    return result
