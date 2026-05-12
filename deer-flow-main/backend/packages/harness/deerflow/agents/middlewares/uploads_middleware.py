"""将上传文件信息注入智能体上下文的中间件。"""

import logging
from pathlib import Path
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.config.paths import Paths, get_paths

logger = logging.getLogger(__name__)


class UploadsMiddlewareState(AgentState):
    """上传中间件的状态模式。"""

    uploaded_files: NotRequired[list[dict] | None]


class UploadsMiddleware(AgentMiddleware[UploadsMiddlewareState]):
    """将上传文件信息注入智能体上下文的中间件。

    从当前消息的 additional_kwargs.files（由前端在上传后设置）中读取文件元数据，
    并在最后一条 human 消息前添加 <uploaded_files> 块，使模型知道有哪些可用文件。
    """

    state_schema = UploadsMiddlewareState

    def __init__(self, base_dir: str | None = None):
        """初始化中间件。

        参数：
            base_dir: 线程数据的基目录。默认使用 Paths 解析。
        """
        super().__init__()
        self._paths = Paths(base_dir) if base_dir else get_paths()

    def _create_files_message(self, new_files: list[dict], historical_files: list[dict]) -> str:
        """创建格式化的上传文件列表消息。

        参数：
            new_files: 当前消息中上传的文件。
            historical_files: 之前消息中上传的文件。

        返回：
            包含在 <uploaded_files> 标签中的格式化字符串。
        """
        lines = ["<uploaded_files>"]

        lines.append("The following files were uploaded in this message:")
        lines.append("")
        if new_files:
            for file in new_files:
                size_kb = file["size"] / 1024
                size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
                lines.append(f"- {file['filename']} ({size_str})")
                lines.append(f"  Path: {file['path']}")
                lines.append("")
        else:
            lines.append("(empty)")

        if historical_files:
            lines.append("The following files were uploaded in previous messages and are still available:")
            lines.append("")
            for file in historical_files:
                size_kb = file["size"] / 1024
                size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
                lines.append(f"- {file['filename']} ({size_str})")
                lines.append(f"  Path: {file['path']}")
                lines.append("")

        lines.append("You can read these files using the `read_file` tool with the paths shown above.")
        lines.append("</uploaded_files>")

        return "\n".join(lines)

    def _files_from_kwargs(self, message: HumanMessage, uploads_dir: Path | None = None) -> list[dict] | None:
        """从消息的 additional_kwargs.files 中提取文件信息。

        前端在成功上传后通过 additional_kwargs.files 发送上传文件元数据。
        每个条目包含：filename、size（字节）、path（虚拟路径）、status。

        参数：
            message: 要检查的 human 消息。
            uploads_dir: 用于验证文件是否存在的物理上传目录。
                         提供时，文件不存在的条目将被跳过。

        返回：
            包含虚拟路径的文件字典列表，如果字段不存在或为空则返回 None。
        """
        kwargs_files = (message.additional_kwargs or {}).get("files")
        if not isinstance(kwargs_files, list) or not kwargs_files:
            return None

        files = []
        for f in kwargs_files:
            if not isinstance(f, dict):
                continue
            filename = f.get("filename") or ""
            if not filename or Path(filename).name != filename:
                continue
            if uploads_dir is not None and not (uploads_dir / filename).is_file():
                continue
            files.append(
                {
                    "filename": filename,
                    "size": int(f.get("size") or 0),
                    "path": f"/mnt/user-data/uploads/{filename}",
                    "extension": Path(filename).suffix,
                }
            )
        return files if files else None

    @override
    def before_agent(self, state: UploadsMiddlewareState, runtime: Runtime) -> dict | None:
        """在智能体执行前注入上传文件信息。

        新文件来自当前消息的 additional_kwargs.files。
        历史文件从线程的上传目录中扫描，排除新文件。

        在最后一条 human 消息内容前添加 <uploaded_files> 上下文。
        原始 additional_kwargs（包括文件元数据）在更新后的消息上保留，
        以便前端可以从流中读取。

        参数：
            state: 当前智能体状态。
            runtime: 包含 thread_id 的运行时上下文。

        返回：
            包含上传文件列表的状态更新。
        """
        messages = list(state.get("messages", []))
        if not messages:
            return None

        last_message_index = len(messages) - 1
        last_message = messages[last_message_index]

        if not isinstance(last_message, HumanMessage):
            return None

        # Resolve uploads directory for existence checks
        thread_id = (runtime.context or {}).get("thread_id")
        uploads_dir = self._paths.sandbox_uploads_dir(thread_id) if thread_id else None

        # 从当前消息的 additional_kwargs.files 获取新上传的文件
        new_files = self._files_from_kwargs(last_message, uploads_dir) or []

        # 从上传目录收集历史文件（排除新文件）
        new_filenames = {f["filename"] for f in new_files}
        historical_files: list[dict] = []
        if uploads_dir and uploads_dir.exists():
            for file_path in sorted(uploads_dir.iterdir()):
                if file_path.is_file() and file_path.name not in new_filenames:
                    stat = file_path.stat()
                    historical_files.append(
                        {
                            "filename": file_path.name,
                            "size": stat.st_size,
                            "path": f"/mnt/user-data/uploads/{file_path.name}",
                            "extension": file_path.suffix,
                        }
                    )

        if not new_files and not historical_files:
            return None

        logger.debug(f"New files: {[f['filename'] for f in new_files]}, historical: {[f['filename'] for f in historical_files]}")

        # 创建文件消息并添加到最后一条 human 消息内容前
        files_message = self._create_files_message(new_files, historical_files)

        # 提取原始内容——处理字符串和列表两种格式
        original_content = ""
        if isinstance(last_message.content, str):
            original_content = last_message.content
        elif isinstance(last_message.content, list):
            text_parts = []
            for block in last_message.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            original_content = "\n".join(text_parts)

        # Create new message with combined content.
        # 保留 additional_kwargs（包括文件元数据），以便前端可以从流式消息中
        # 读取结构化的文件信息。
        updated_message = HumanMessage(
            content=f"{files_message}\n\n{original_content}",
            id=last_message.id,
            additional_kwargs=last_message.additional_kwargs,
        )

        messages[last_message_index] = updated_message

        return {
            "uploaded_files": new_files,
            "messages": messages,
        }
