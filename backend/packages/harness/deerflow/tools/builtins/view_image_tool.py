"""图片查看工具（View Image Tool）

本模块实现了 `view_image` 工具，用于读取图片文件并将其转换为 base64 编码，
供具有视觉能力的模型查看。

功能说明：
--------
- 读取指定路径的图片文件
- 验证图片格式（jpg/jpeg/png/webp）
- 检测实际 MIME 类型与文件扩展名是否匹配
- 将图片转换为 base64 编码
- 通过状态更新将图片数据传递给模型

安全限制：
--------
1. **路径限制**：只能读取以下虚拟路径下的图片：
   - /mnt/user-data/workspace
   - /mnt/user-data/uploads
   - /mnt/user-data/outputs
2. **大小限制**：图片最大 20MB（_MAX_IMAGE_BYTES）
3. **格式限制**：仅支持 jpg、jpeg、png、webp 格式
4. **沙箱验证**：通过 validate_local_tool_path 和 resolve_and_validate_user_data_path
   进行路径安全性验证
5. **错误信息脱敏**：错误信息中的本地路径会被遮蔽

MIME 类型检测：
-------------
采用双重检测策略：
1. **扩展名检测**：通过文件扩展名映射预期 MIME 类型
2. **魔数检测**：读取文件头字节验证实际格式

两者必须匹配，否则返回错误。支持的格式：
- JPEG：以 0xFF D8 FF 开头
- PNG：以 0x89 PNG\r\n\x1A\n 开头
- WEBP：以 RIFF 开头，偏移 8-12 为 WEBP

加载条件：
--------
仅当模型的 supports_vision 配置为 True 时，此工具才会被加载到工具列表中。
参见 tools.py 中的 get_available_tools() 函数。

状态更新：
--------
工具返回一个 Command 对象，包含：
- viewed_images：以图片路径为键、包含 base64 和 mime_type 的字典
- messages：成功消息（ToolMessage）

merge_viewed_images reducer 负责将新图片合并到现有状态中。
"""

import base64
import mimetypes
from pathlib import Path
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.tools.types import Runtime

# 允许的图片虚拟路径根目录
_ALLOWED_IMAGE_VIRTUAL_ROOTS = (
    f"{VIRTUAL_PATH_PREFIX}/workspace",
    f"{VIRTUAL_PATH_PREFIX}/uploads",
    f"{VIRTUAL_PATH_PREFIX}/outputs",
)
_ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT = ", ".join(_ALLOWED_IMAGE_VIRTUAL_ROOTS)

# 图片文件最大字节数（20MB）
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 文件扩展名到 MIME 类型的映射
_EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _is_allowed_image_virtual_path(image_path: str) -> bool:
    """检查图片路径是否在允许的虚拟路径范围内。

    只有 /mnt/user-data/workspace、/mnt/user-data/uploads、
    /mnt/user-data/outputs 下的图片才允许读取。
    """
    return any(image_path == root or image_path.startswith(f"{root}/") for root in _ALLOWED_IMAGE_VIRTUAL_ROOTS)


def _detect_image_mime(image_data: bytes) -> str | None:
    """通过文件头魔数检测图片的实际 MIME 类型。

    检测逻辑：
    - JPEG：以 0xFF D8 FF 开头
    - PNG：以 0x89 PNG\r\n\x1A\n 开头（8 字节签名）
    - WEBP：以 RIFF 开头，偏移 8-12 为 WEBP

    Args:
        image_data: 图片文件的原始字节数据

    Returns:
        检测到的 MIME 类型字符串，如果无法识别则返回 None
    """
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_data) >= 12 and image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sanitize_image_error(error: Exception, thread_data: ThreadDataState | None) -> str:
    """清理错误信息中的本地路径，防止向用户暴露服务器内部路径。

    使用 mask_local_paths_in_output 将实际的文件系统路径
    替换为虚拟路径，确保错误信息对用户友好且安全。
    """
    from deerflow.sandbox.tools import mask_local_paths_in_output

    return mask_local_paths_in_output(f"{type(error).__name__}: {error}", thread_data)


@tool("view_image", parse_docstring=True)
def view_image_tool(
    runtime: Runtime,
    image_path: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Read an image file.

    读取图片文件。

    何时使用 view_image 工具：
    - 当需要查看图片文件时。

    何时不应使用 view_image 工具：
    - 非图片文件（使用 present_files 代替）
    - 同时处理多个文件（使用 present_files 代替）

    Args:
        image_path: 图片文件的绝对 /mnt/user-data 虚拟路径。
                    支持的格式：jpg、jpeg、png、webp。
    """
    from deerflow.sandbox.exceptions import SandboxRuntimeError
    from deerflow.sandbox.tools import (
        get_thread_data,
        resolve_and_validate_user_data_path,
        validate_local_tool_path,
    )

    thread_data = get_thread_data(runtime)

    # 验证路径是否在允许的范围内
    if not _is_allowed_image_virtual_path(image_path):
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error: Only image paths under {_ALLOWED_IMAGE_VIRTUAL_ROOTS_TEXT} are allowed",
                        tool_call_id=tool_call_id,
                    )
                ]
            },
        )

    # 沙箱路径验证
    try:
        validate_local_tool_path(image_path, thread_data, read_only=True)
        actual_path = resolve_and_validate_user_data_path(image_path, thread_data)
    except (PermissionError, SandboxRuntimeError) as e:
        return Command(
            update={"messages": [ToolMessage(f"Error: {str(e)}", tool_call_id=tool_call_id)]},
        )

    path = Path(actual_path)

    # 验证文件是否存在
    if not path.exists():
        return Command(
            update={"messages": [ToolMessage(f"Error: Image file not found: {image_path}", tool_call_id=tool_call_id)]},
        )

    # 验证是否为文件（非目录）
    if not path.is_file():
        return Command(
            update={"messages": [ToolMessage(f"Error: Path is not a file: {image_path}", tool_call_id=tool_call_id)]},
        )

    # 验证图片扩展名是否受支持
    expected_mime_type = _EXTENSION_TO_MIME.get(path.suffix.lower())
    if expected_mime_type is None:
        return Command(
            update={"messages": [ToolMessage(f"Error: Unsupported image format: {path.suffix}. Supported formats: {', '.join(_EXTENSION_TO_MIME)}", tool_call_id=tool_call_id)]},
        )

    # 通过文件扩展名检测 MIME 类型
    mime_type, _ = mimetypes.guess_type(actual_path)
    if mime_type is None:
        mime_type = expected_mime_type

    # 检查文件大小
    try:
        image_size = path.stat().st_size
    except OSError as e:
        return Command(
            update={"messages": [ToolMessage(f"Error reading image metadata: {_sanitize_image_error(e, thread_data)}", tool_call_id=tool_call_id)]},
        )
    if image_size > _MAX_IMAGE_BYTES:
        return Command(
            update={"messages": [ToolMessage(f"Error: Image file is too large: {image_size} bytes. Maximum supported size is {_MAX_IMAGE_BYTES} bytes", tool_call_id=tool_call_id)]},
        )

    # 读取图片文件并转换为 base64
    try:
        with open(actual_path, "rb") as f:
            image_data = f.read()
    except Exception as e:
        return Command(
            update={"messages": [ToolMessage(f"Error reading image file: {_sanitize_image_error(e, thread_data)}", tool_call_id=tool_call_id)]},
        )

    # 验证文件内容是否为支持的图片格式（通过魔数检测）
    detected_mime_type = _detect_image_mime(image_data)
    if detected_mime_type is None:
        return Command(
            update={"messages": [ToolMessage("Error: File contents do not match a supported image format", tool_call_id=tool_call_id)]},
        )
    # 验证实际格式与扩展名是否匹配
    if detected_mime_type != expected_mime_type:
        return Command(
            update={"messages": [ToolMessage(f"Error: Image contents are {detected_mime_type}, but file extension indicates {expected_mime_type}", tool_call_id=tool_call_id)]},
        )
    mime_type = detected_mime_type

    # 将图片数据编码为 base64
    image_base64 = base64.b64encode(image_data).decode("utf-8")

    # 更新 viewed_images 状态
    # merge_viewed_images reducer 会处理与已有图片的合并
    new_viewed_images = {image_path: {"base64": image_base64, "mime_type": mime_type}}

    return Command(
        update={"viewed_images": new_viewed_images, "messages": [ToolMessage("Successfully read image", tool_call_id=tool_call_id)]},
    )
