"""悬挂工具调用修补中间件 — 在模型调用前检测并修补缺失的 ToolMessage。

悬挂工具调用（dangling tool call）是指：
  AIMessage 包含 tool_calls，但消息历史中没有对应的 ToolMessage
  （例如用户中断或请求取消后，工具结果丢失）。
  这会导致 LLM 因消息格式不完整而报错。

修补策略：
  扫描消息历史，为每个缺少 ToolMessage 的 tool_call 注入合成错误响应：
  - 普通悬挂："[Tool call was interrupted and did not return a result.]"
  - 无效工具调用："[Tool call could not be executed because its arguments were invalid: {error}]"

为什么用 wrap_model_call 而非 before_model：
  wrap_model_call 可以精确控制修补消息的插入位置（紧跟在对应 AIMessage 之后），
  before_model + add_messages reducer 只能追加到消息列表末尾，位置不正确。

工具调用来源归一化：
  _message_tool_calls() 同时检查三个来源：
  1. msg.tool_calls（结构化字段，主要来源）
  2. msg.additional_kwargs["tool_calls"]（原始提供者载荷，某些 provider 只走这个）
  3. msg.invalid_tool_calls（格式错误的调用，仍需匹配 ToolMessage）
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


class DanglingToolCallMiddleware(AgentMiddleware[AgentState]):
    """Inserts placeholder ToolMessages for dangling tool calls before model invocation.

    Scans the message history for AIMessages whose tool_calls lack corresponding
    ToolMessages, and injects synthetic error responses immediately after the
    offending AIMessage so the LLM receives a well-formed conversation.
    """

    @staticmethod
    def _message_tool_calls(msg) -> list[dict]:
        """Return normalized tool calls from structured fields or raw provider payloads.

        LangChain stores malformed provider function calls in ``invalid_tool_calls``.
        They do not execute, but provider adapters may still serialize enough of
        the call id/name back into the next request that strict OpenAI-compatible
        validators expect a matching ToolMessage. Treat them as dangling calls so
        the next model request stays well-formed and the model sees a recoverable
        tool error instead of another provider 400.
        """
        normalized: list[dict] = []

        tool_calls = getattr(msg, "tool_calls", None) or []
        normalized.extend(list(tool_calls))

        raw_tool_calls = (getattr(msg, "additional_kwargs",
                          None) or {}).get("tool_calls") or []
        if not tool_calls:
            for raw_tc in raw_tool_calls:
                if not isinstance(raw_tc, dict):
                    continue

                function = raw_tc.get("function")
                name = raw_tc.get("name")
                if not name and isinstance(function, dict):
                    name = function.get("name")

                args = raw_tc.get("args", {})
                if not args and isinstance(function, dict):
                    raw_args = function.get("arguments")
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            parsed_args = {}
                        args = parsed_args if isinstance(
                            parsed_args, dict) else {}

                normalized.append(
                    {
                        "id": raw_tc.get("id"),
                        "name": name or "unknown",
                        "args": args if isinstance(args, dict) else {},
                    }
                )

        for invalid_tc in getattr(msg, "invalid_tool_calls", None) or []:
            if not isinstance(invalid_tc, dict):
                continue
            normalized.append(
                {
                    "id": invalid_tc.get("id"),
                    "name": invalid_tc.get("name") or "unknown",
                    "args": {},
                    "invalid": True,
                    "error": invalid_tc.get("error"),
                }
            )

        return normalized

    @staticmethod
    def _synthetic_tool_message_content(tool_call: dict) -> str:
        if tool_call.get("invalid"):
            error = tool_call.get("error")
            if isinstance(error, str) and error:
                return f"[Tool call could not be executed because its arguments were invalid: {error}]"
            return "[Tool call could not be executed because its arguments were invalid.]"
        return "[Tool call was interrupted and did not return a result.]"

    def _build_patched_messages(self, messages: list) -> list | None:
        """Return messages with tool results grouped after their tool-call AIMessage.

        This normalizes model-bound causal order before provider serialization while
        preserving already-valid transcripts unchanged.
        """
        tool_messages_by_id: dict[str, ToolMessage] = {}
        for msg in messages:
            if isinstance(msg, ToolMessage):
                tool_messages_by_id.setdefault(msg.tool_call_id, msg)

        tool_call_ids: set[str] = set()
        for msg in messages:
            if getattr(msg, "type", None) != "ai":
                continue
            for tc in self._message_tool_calls(msg):
                tc_id = tc.get("id")
                if tc_id:
                    tool_call_ids.add(tc_id)

        patched: list = []
        consumed_tool_msg_ids: set[str] = set()
        patch_count = 0
        for msg in messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id in tool_call_ids:
                continue

            patched.append(msg)
            if getattr(msg, "type", None) != "ai":
                continue

            for tc in self._message_tool_calls(msg):
                tc_id = tc.get("id")
                if not tc_id or tc_id in consumed_tool_msg_ids:
                    continue

                existing_tool_msg = tool_messages_by_id.get(tc_id)
                if existing_tool_msg is not None:
                    patched.append(existing_tool_msg)
                    consumed_tool_msg_ids.add(tc_id)
                else:
                    patched.append(
                        ToolMessage(
                            content=self._synthetic_tool_message_content(tc),
                            tool_call_id=tc_id,
                            name=tc.get("name", "unknown"),
                            status="error",
                        )
                    )
                    consumed_tool_msg_ids.add(tc_id)
                    patch_count += 1

        if patched == messages:
            return None

        if patch_count:
            logger.warning(
                f"Injecting {patch_count} placeholder ToolMessage(s) for dangling tool calls")
        return patched

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(request.messages)
        if patched is not None:
            request = request.override(messages=patched)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(request.messages)
        if patched is not None:
            request = request.override(messages=patched)
        return await handler(request)
