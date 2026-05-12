"""SandboxAuditMiddleware - bash 命令安全审计中间件。"""

import json
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 命令分类规则
# ---------------------------------------------------------------------------

# 每个模式在导入时编译一次。
_HIGH_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-[^\s]*r[^\s]*\s+(/\*?|~/?\*?|/home\b|/root\b)\s*$"),  # rm -rf / /* ~ /home /root
    re.compile(r"(curl|wget).+\|\s*(ba)?sh"),  # curl|sh, wget|sh
    re.compile(r"dd\s+if="),
    re.compile(r"mkfs"),
    re.compile(r"cat\s+/etc/shadow"),
    re.compile(r">\s*/etc/"),  # overwrite /etc/ files
]

_MEDIUM_RISK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"chmod\s+777"),  # overly permissive, but reversible
    re.compile(r"pip\s+install"),
    re.compile(r"pip3\s+install"),
    re.compile(r"apt(-get)?\s+install"),
]


def _classify_command(command: str) -> str:
    """返回 'block'、'warn' 或 'pass'。"""
    # 标准化匹配（折叠空白字符）
    normalized = " ".join(command.split())

    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(normalized):
            return "block"

    # 也尝试使用 shlex 解析的 token 进行高风险检测
    try:
        tokens = shlex.split(command)
        joined = " ".join(tokens)
        for pattern in _HIGH_RISK_PATTERNS:
            if pattern.search(joined):
                return "block"
    except ValueError:
        # shlex.split 在未闭合引号时会失败——视为可疑
        return "block"

    for pattern in _MEDIUM_RISK_PATTERNS:
        if pattern.search(normalized):
            return "warn"

    return "pass"


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------


class SandboxAuditMiddleware(AgentMiddleware[ThreadState]):
    """bash 命令安全审计中间件。

    对于每个 ``bash`` 工具调用：
    1. **命令分类**：通过正则 + shlex 分析将命令分为
       高风险（阻止）、中风险（警告）或安全（通过）。
    2. **审计日志**：每次 bash 调用都通过标准日志记录器记录为结构化 JSON 条目
       （可在 langgraph.log 中查看）。

    高风险命令（如 ``rm -rf /``、``curl url | bash``）会被阻止：
    不调用处理程序，而是返回错误 ``ToolMessage``，使智能体循环可以优雅地继续。

    中风险命令（如 ``pip install``、``chmod 777``）会正常执行；
    但会在工具结果中追加警告，以便 LLM 知晓。
    """

    state_schema = ThreadState

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_thread_id(self, request: ToolCallRequest) -> str | None:
        runtime = request.runtime  # ToolRuntime；在测试中可能为 None
        if runtime is None:
            return None
        ctx = getattr(runtime, "context", None) or {}
        thread_id = ctx.get("thread_id") if isinstance(ctx, dict) else None
        if thread_id is None:
            cfg = getattr(runtime, "config", None) or {}
            thread_id = cfg.get("configurable", {}).get("thread_id")
        return thread_id

    def _write_audit(self, thread_id: str | None, command: str, verdict: str) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "thread_id": thread_id or "unknown",
            "command": command,
            "verdict": verdict,
        }
        logger.info("[SandboxAudit] %s", json.dumps(record, ensure_ascii=False))

    def _build_block_message(self, request: ToolCallRequest, reason: str) -> ToolMessage:
        tool_call_id = str(request.tool_call.get("id") or "missing_id")
        return ToolMessage(
            content=f"Command blocked: {reason}. Please use a safer alternative approach.",
            tool_call_id=tool_call_id,
            name="bash",
            status="error",
        )

    def _append_warn_to_result(self, result: ToolMessage | Command, command: str) -> ToolMessage | Command:
        """为中风险命令在工具结果中追加警告说明。"""
        if not isinstance(result, ToolMessage):
            return result
        warning = f"\n\n⚠️ Warning: `{command}` is a medium-risk command that may modify the runtime environment."
        if isinstance(result.content, list):
            new_content = list(result.content) + [{"type": "text", "text": warning}]
        else:
            new_content = str(result.content) + warning
        return ToolMessage(
            content=new_content,
            tool_call_id=result.tool_call_id,
            name=result.name,
            status=result.status,
        )

    # ------------------------------------------------------------------
    # 核心逻辑（同步和异步路径共享）
    # ------------------------------------------------------------------

    def _pre_process(self, request: ToolCallRequest) -> tuple[str, str | None, str]:
        """
        返回 (command, thread_id, verdict)。
        verdict 为 'block'、'warn' 或 'pass'。
        """
        args = request.tool_call.get("args", {})
        command: str = args.get("command", "")
        thread_id = self._get_thread_id(request)

        # ① 分类命令
        verdict = _classify_command(command)

        # ② 审计日志
        self._write_audit(thread_id, command, verdict)

        if verdict == "block":
            logger.warning("[SandboxAudit] BLOCKED thread=%s cmd=%r", thread_id, command)
        elif verdict == "warn":
            logger.warning("[SandboxAudit] WARN (medium-risk) thread=%s cmd=%r", thread_id, command)

        return command, thread_id, verdict

    # ------------------------------------------------------------------
    # wrap_tool_call 钩子
    # ------------------------------------------------------------------

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "bash":
            return handler(request)

        command, _, verdict = self._pre_process(request)
        if verdict == "block":
            return self._build_block_message(request, "security violation detected")
        result = handler(request)
        if verdict == "warn":
            result = self._append_warn_to_result(result, command)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "bash":
            return await handler(request)

        command, _, verdict = self._pre_process(request)
        if verdict == "block":
            return self._build_block_message(request, "security violation detected")
        result = await handler(request)
        if verdict == "warn":
            result = self._append_warn_to_result(result, command)
        return result
