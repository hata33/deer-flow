"""OpenAI Codex 提供商 — 通过 ChatGPT Codex Responses API 接入模型。

模块功能
========
为 DeerFlow 提供 OpenAI Codex 模型的接入能力，使用 ChatGPT 的 Codex Responses API
（chatgpt.com/backend-api/codex/responses）作为后端端点。这是 Codex CLI 工具内部
使用的同一端点，允许 DeerFlow 直接复用 Codex CLI 的 OAuth 凭证。

核心设计
========
1. **Responses API 格式**: 不使用传统的 Chat Completions API，而是采用 OpenAI 的
   Responses API 格式，后者将系统提示作为 `instructions` 字段传递，对话历史作为
   `input` 数组传递，工具调用使用 `function_call` / `function_call_output` 类型。
2. **SSE 流式收集**: Codex 端点强制要求 stream=True，因此即使我们只需要完整响应，
   也必须通过 SSE（Server-Sent Events）流式读取并收集最终结果。
3. **凭证自动加载**: 从 ~/.codex/auth.json 自动加载 Codex CLI 的认证凭证，
   支持新旧两种 token 存储格式（顶层字段和嵌套 tokens 对象）。
4. **指数退避重试**: 对 429（速率限制）、500（服务端错误）和 529（过载）状态码
   实现指数退避重试策略。
5. **推理努力级别**: 支持 reasoning_effort 参数（none/low/medium/high），
   控制模型的推理深度和资源消耗。

关键特性
========
- 自动从 ~/.codex/auth.json 或 $CODEX_AUTH_PATH 加载凭证
- 将 LangChain 消息格式转换为 Codex Responses API 格式
- 将 LangChain 工具定义转换为 Responses API 的 function 格式
- 解析 SSE 流中的 response.output_item.done 和 response.completed 事件
- 支持 reasoning（推理摘要）提取并映射到 reasoning_content
- 支持工具调用的参数解析和错误处理

使用场景
========
在 config.yaml 中配置 Codex 模型::

    - name: gpt-5.4
      use: deerflow.models.openai_codex_provider:CodexChatModel
      model: gpt-5.4
      reasoning_effort: medium

注意事项
========
- 必须先通过 Codex CLI 登录生成 ~/.codex/auth.json 凭证文件
- 端点强制使用流式传输（stream=True），即使只需要完整响应
- 不支持 max_tokens / max_output_tokens 参数，factory 会自动移除
- 工具调用参数必须是有效的 JSON 对象，否则会被标记为 invalid_tool_call
"""

import json
import logging
import time
from typing import Any

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deerflow.models.credential_loader import CodexCliCredential, load_codex_cli_credential

logger = logging.getLogger(__name__)

# Codex Responses API 的基础 URL，与 Codex CLI 工具使用的端点一致
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _build_usage_metadata(oai_usage: dict) -> dict:
    """将 Codex/Responses API 的 usage 字典转换为 LangChain usage_metadata 格式。

    OpenAI Responses API 的 usage 结构与 Chat Completions API 略有不同，
    此函数将其映射为 LangChain AIMessage.usage_metadata 所期望的字典结构，
    避免依赖 langchain_openai 的私有辅助函数。

    Args:
        oai_usage: Codex Responses API 返回的 usage 字典，包含 input_tokens、
            output_tokens 等字段。

    Returns:
        dict: LangChain 格式的 usage_metadata 字典，包含 input_tokens、
            output_tokens、total_tokens 以及可选的缓存和推理详情。
    """
    input_tokens = oai_usage.get("input_tokens", 0)
    output_tokens = oai_usage.get("output_tokens", 0)
    total_tokens = oai_usage.get("total_tokens", input_tokens + output_tokens)
    metadata: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    # 提取输入 token 的缓存命中详情（如果有的话）
    input_details = oai_usage.get("input_tokens_details") or {}
    # 提取输出 token 的推理消耗详情（如果有的话）
    output_details = oai_usage.get("output_tokens_details") or {}
    cache_read = input_details.get("cached_tokens")
    if cache_read is not None:
        metadata["input_token_details"] = {"cache_read": cache_read}
    reasoning = output_details.get("reasoning_tokens")
    if reasoning is not None:
        metadata["output_token_details"] = {"reasoning": reasoning}
    return metadata


# 最大重试次数：针对速率限制和服务端错误
MAX_RETRIES = 3


class CodexChatModel(BaseChatModel):
    """通过 ChatGPT Codex Responses API 接入的 LangChain 聊天模型。

    本类直接继承 LangChain 的 BaseChatModel（而非 ChatOpenAI），因为 Codex
    Responses API 的协议与标准 Chat Completions API 差异较大，无法复用
    ChatOpenAI 的请求/响应处理逻辑。

    核心特性：
    - 自动从 Codex CLI 的认证文件加载 OAuth 凭证
    - 将 LangChain 消息格式转换为 Codex Responses API 格式
    - 通过 SSE 流式收集最终响应
    - 支持推理努力级别控制（reasoning_effort）
    - 支持工具调用和推理摘要提取

    Attributes:
        model: 模型名称（如 gpt-5.4）。
        reasoning_effort: 推理努力级别（none/low/medium/high）。
        retry_max_attempts: 最大重试次数。

    配置示例::

        - name: gpt-5.4
          use: deerflow.models.openai_codex_provider:CodexChatModel
          model: gpt-5.4
          reasoning_effort: medium
    """

    model: str = "gpt-5.4"
    reasoning_effort: str = "medium"
    retry_max_attempts: int = MAX_RETRIES
    _access_token: str = ""   # OAuth 访问令牌（从 ~/.codex/auth.json 加载）
    _account_id: str = ""     # ChatGPT 账户 ID（用于 ChatGPT-Account-ID 请求头）

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """声明本类支持 LangChain 序列化。"""
        return True

    @property
    def _llm_type(self) -> str:
        """返回模型类型标识符。"""
        return "codex-responses"

    def _validate_retry_config(self) -> None:
        """验证重试配置的合法性。

        确保重试次数至少为 1，避免配置错误导致无法重试。

        Raises:
            ValueError: 当 retry_max_attempts < 1 时抛出。
        """
        if self.retry_max_attempts < 1:
            raise ValueError("retry_max_attempts must be >= 1")

    def model_post_init(self, __context: Any) -> None:
        """模型初始化后处理：自动加载 Codex CLI 凭证。

        从 ~/.codex/auth.json 或 $CODEX_AUTH_PATH 指定的路径加载认证信息。
        如果找不到有效凭证，将抛出 ValueError 阻止模型创建。

        Args:
            __context: Pydantic 模型初始化上下文（由框架传入）。

        Raises:
            ValueError: 当找不到 Codex CLI 凭证时抛出。
        """
        self._validate_retry_config()

        cred = self._load_codex_auth()
        if cred:
            self._access_token = cred.access_token
            self._account_id = cred.account_id
            logger.info(f"Using Codex CLI credential (account: {self._account_id[:8]}...)")
        else:
            raise ValueError("Codex CLI credential not found. Expected ~/.codex/auth.json or CODEX_AUTH_PATH.")

        super().model_post_init(__context)

    def _load_codex_auth(self) -> CodexCliCredential | None:
        """从 Codex CLI 认证文件加载凭证。

        Returns:
            CodexCliCredential | None: 成功返回凭证对象，失败返回 None。
        """
        return load_codex_cli_credential()

    @classmethod
    def _normalize_content(cls, content: Any) -> str:
        """将 LangChain 的多种内容格式扁平化为纯文本字符串。

        LangChain 的消息内容可能是字符串、列表（多模态）或字典（结构化内容），
        而 Codex API 仅接受字符串格式。此方法递归地将各种格式统一为纯文本。

        Args:
            content: LangChain 消息内容，支持 str、list、dict 等多种格式。

        Returns:
            str: 扁平化后的纯文本字符串。
        """
        if isinstance(content, str):
            return content

        # 递归处理列表中的每个元素，用换行符连接
        if isinstance(content, list):
            parts = [cls._normalize_content(item) for item in content]
            return "\n".join(part for part in parts if part)

        # 从字典中提取文本内容，优先查找 text 和 output 字段
        if isinstance(content, dict):
            for key in ("text", "output"):
                value = content.get(key)
                if isinstance(value, str):
                    return value
            # 递归处理嵌套的 content 字段
            nested_content = content.get("content")
            if nested_content is not None:
                return cls._normalize_content(nested_content)
            # 兜底：序列化为 JSON 字符串
            try:
                return json.dumps(content, ensure_ascii=False)
            except TypeError:
                return str(content)

        # 最终兜底：尝试 JSON 序列化，失败则使用 str()
        try:
            return json.dumps(content, ensure_ascii=False)
        except TypeError:
            return str(content)

    def _convert_messages(self, messages: list[BaseMessage]) -> tuple[str, list[dict]]:
        """将 LangChain 消息列表转换为 Codex Responses API 格式。

        Codex Responses API 使用不同的消息结构：
        - SystemMessage → instructions 字符串（非消息数组中的条目）
        - HumanMessage → {role: "user", content: ...}
        - AIMessage → {role: "assistant", content: ...} + function_call 条目
        - ToolMessage → {type: "function_call_output", call_id: ..., output: ...}

        Args:
            messages: LangChain 消息列表。

        Returns:
            tuple[str, list[dict]]: 元组包含两个元素：
                - str: 合并后的系统指令（instructions）
                - list[dict]: Codex API 格式的 input 条目列表
        """
        instructions_parts: list[str] = []
        input_items = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                # 系统消息提取为 instructions，而非放入 input 数组
                content = self._normalize_content(msg.content)
                if content:
                    instructions_parts.append(content)
            elif isinstance(msg, HumanMessage):
                content = self._normalize_content(msg.content)
                input_items.append({"role": "user", "content": content})
            elif isinstance(msg, AIMessage):
                # AI 消息可能同时包含文本内容和工具调用
                if msg.content:
                    content = self._normalize_content(msg.content)
                    input_items.append({"role": "assistant", "content": content})
                # 工具调用转换为 Responses API 的 function_call 格式
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        input_items.append(
                            {
                                "type": "function_call",
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"]) if isinstance(tc["args"], dict) else tc["args"],
                                "call_id": tc["id"],
                            }
                        )
            elif isinstance(msg, ToolMessage):
                # 工具执行结果转换为 function_call_output 格式
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id,
                        "output": self._normalize_content(msg.content),
                    }
                )

        # 合并所有系统消息为单个 instructions 字符串
        instructions = "\n\n".join(instructions_parts) or "You are a helpful assistant."

        return instructions, input_items

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """将 LangChain 工具格式转换为 Codex Responses API 格式。

        LangChain 使用 OpenAI Chat Completions 的 function 格式（嵌套 function 对象），
        而 Codex Responses API 使用扁平的 function 格式（name、description、parameters
        在顶层）。

        Args:
            tools: LangChain 格式的工具定义列表。

        Returns:
            list[dict]: Codex Responses API 格式的工具定义列表。
        """
        responses_tools = []
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                # 从嵌套的 function 对象中提取字段
                fn = tool["function"]
                responses_tools.append(
                    {
                        "type": "function",
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                )
            elif "name" in tool:
                # 已经是扁平格式，直接使用
                responses_tools.append(
                    {
                        "type": "function",
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    }
                )
        return responses_tools

    def _call_codex_api(self, messages: list[BaseMessage], tools: list[dict] | None = None) -> dict:
        """调用 Codex Responses API 并返回完整响应。

        构建 API 请求负载，设置认证头，并执行带重试的 SSE 流式请求。
        Codex 端点强制要求 stream=True，因此必须通过流式方式收集完整响应。

        重试策略：
        - 仅对 429（速率限制）、500（服务端错误）、529（过载）进行重试
        - 采用指数退避算法（2000ms × 2^(attempt-1)）
        - 其他状态码直接抛出异常

        Args:
            messages: LangChain 消息列表。
            tools: 可选的工具定义列表。

        Returns:
            dict: Codex API 的完整响应字典。

        Raises:
            httpx.HTTPStatusError: 超过重试上限后的 HTTP 错误。
        """
        instructions, input_items = self._convert_messages(messages)

        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "store": False,   # 不在 Codex 服务端存储对话历史
            "stream": True,   # Codex 端点强制要求流式传输
            # reasoning_effort 为 "none" 时不生成推理摘要，节省 token
            "reasoning": {"effort": self.reasoning_effort, "summary": "detailed"} if self.reasoning_effort != "none" else {"effort": "none"},
        }

        if tools:
            payload["tools"] = self._convert_tools(tools)

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "ChatGPT-Account-ID": self._account_id,  # Codex API 要求的账户标识
            "Content-Type": "application/json",
            "Accept": "text/event-stream",  # SSE 流式响应
            "originator": "codex_cli_rs",   # 标识请求来源，与 Codex CLI 保持一致
        }

        last_error = None
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                return self._stream_response(headers, payload)
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code in (429, 500, 529):
                    # 可重试的状态码：速率限制、服务端错误、过载
                    if attempt >= self.retry_max_attempts:
                        raise
                    wait_ms = 2000 * (1 << (attempt - 1))
                    logger.warning(f"Codex API error {e.response.status_code}, retrying {attempt}/{self.retry_max_attempts} after {wait_ms}ms")
                    time.sleep(wait_ms / 1000)
                else:
                    # 不可重试的状态码，直接抛出
                    raise
            except Exception:
                raise

        raise last_error

    def _stream_response(self, headers: dict, payload: dict) -> dict:
        """通过 SSE 流式读取 Codex API 响应并收集最终结果。

        Codex 端点以 Server-Sent Events 格式返回响应，包含两类关键事件：
        - response.output_item.done: 每个 output 条目完成时触发
        - response.completed: 整个响应完成时触发

        由于 Codex 有时只在流事件中输出最终内容（response.completed 时的 output
        可能为空），因此需要同时收集流事件中的 output 条目，并在最终响应中合并。

        Args:
            headers: HTTP 请求头字典。
            payload: API 请求负载字典。

        Returns:
            dict: 完整的 Codex API 响应字典。

        Raises:
            RuntimeError: 当流结束但未收到 response.completed 事件时抛出。
        """
        completed_response = None
        # 按索引收集流式输出条目，确保顺序正确
        streamed_output_items: dict[int, dict[str, Any]] = {}

        with httpx.Client(timeout=300) as client:
            with client.stream("POST", f"{CODEX_BASE_URL}/responses", headers=headers, json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    data = self._parse_sse_data_line(line)
                    if not data:
                        continue

                    event_type = data.get("type")
                    if event_type == "response.output_item.done":
                        # 收集每个完成的 output 条目，用于后续合并
                        output_index = data.get("output_index")
                        output_item = data.get("item")
                        if isinstance(output_index, int) and isinstance(output_item, dict):
                            streamed_output_items[output_index] = output_item
                    elif event_type == "response.completed":
                        # 整个响应完成，获取最终的 response 对象
                        completed_response = data["response"]

        if not completed_response:
            raise RuntimeError("Codex API stream ended without response.completed event")

        # ChatGPT Codex 可能只在流事件中输出最终内容。
        # 当 response.completed 到达时，response.output 可能仍然为空，
        # 因此需要将流式收集的条目合并到最终响应中。
        if streamed_output_items:
            merged_output = []
            response_output = completed_response.get("output")
            if isinstance(response_output, list):
                merged_output = list(response_output)

            # 扩展列表以容纳所有流式条目的索引位置
            max_index = max(max(streamed_output_items), len(merged_output) - 1)
            if max_index >= 0 and len(merged_output) <= max_index:
                merged_output.extend([None] * (max_index + 1 - len(merged_output)))

            # 将流式条目填入对应位置，覆盖可能的空位
            for output_index, output_item in streamed_output_items.items():
                existing_item = merged_output[output_index]
                if not isinstance(existing_item, dict):
                    merged_output[output_index] = output_item

            completed_response = dict(completed_response)
            # 过滤掉 None 占位符
            completed_response["output"] = [item for item in merged_output if isinstance(item, dict)]

        return completed_response

    @staticmethod
    def _parse_sse_data_line(line: str) -> dict[str, Any] | None:
        """解析 SSE 流中的 data 行。

        SSE 格式要求每行以 "data:" 开头，终端标记为 "data: [DONE]"。
        此方法提取 data 后的 JSON 内容，跳过非 data 行和终端标记。

        Args:
            line: SSE 流中的单行文本。

        Returns:
            dict | None: 解析成功返回 JSON 字典，跳过时返回 None。
        """
        if not line.startswith("data:"):
            return None

        raw_data = line[5:].strip()
        # 跳过空行和终端标记
        if not raw_data or raw_data == "[DONE]":
            return None

        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            logger.debug(f"Skipping non-JSON Codex SSE frame: {raw_data}")
            return None

        return data if isinstance(data, dict) else None

    def _parse_tool_call_arguments(self, output_item: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """解析工具调用的参数，安全处理格式错误。

        Codex API 返回的工具调用参数可能是字典（已解析）或字符串（JSON 待解析）。
        此方法统一处理这两种情况，并在解析失败时生成 invalid_tool_call 条目，
        而非直接抛出异常导致整个请求失败。

        Args:
            output_item: Codex API 返回的 function_call output 条目。

        Returns:
            tuple[dict | None, dict | None]: 元组包含两个元素：
                - 第一个为成功解析的参数字典（失败时为 None）
                - 第二个为无效工具调用信息（成功时为 None）
        """
        raw_arguments = output_item.get("arguments", "{}")
        # 如果参数已经是字典类型（某些 SDK 版本会预解析），直接返回
        if isinstance(raw_arguments, dict):
            return raw_arguments, None

        normalized_arguments = raw_arguments or "{}"
        try:
            parsed_arguments = json.loads(normalized_arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            # JSON 解析失败，生成错误描述而非抛出异常
            return None, {
                "type": "invalid_tool_call",
                "name": output_item.get("name"),
                "args": str(raw_arguments),
                "id": output_item.get("call_id"),
                "error": f"Failed to parse tool arguments: {exc}",
            }

        # 参数必须解析为 JSON 对象（字典），不能是数组或基本类型
        if not isinstance(parsed_arguments, dict):
            return None, {
                "type": "invalid_tool_call",
                "name": output_item.get("name"),
                "args": str(raw_arguments),
                "id": output_item.get("call_id"),
                "error": "Tool arguments must decode to a JSON object.",
            }

        return parsed_arguments, None

    def _parse_response(self, response: dict) -> ChatResult:
        """将 Codex Responses API 的响应解析为 LangChain ChatResult。

        Codex 的 output 数组包含多种类型的条目：
        - reasoning: 推理过程摘要
        - message: 最终文本输出
        - function_call: 工具调用

        此方法遍历 output 条目，提取文本内容、推理内容和工具调用，
        并构建 LangChain 标准的 ChatResult 对象。

        Args:
            response: Codex API 返回的完整响应字典。

        Returns:
            ChatResult: LangChain 格式的聊天结果，包含 AIMessage 和 token 用量。
        """
        content = ""
        tool_calls = []
        invalid_tool_calls = []
        reasoning_content = ""

        for output_item in response.get("output", []):
            if output_item.get("type") == "reasoning":
                # 提取推理摘要文本
                for summary_item in output_item.get("summary", []):
                    if isinstance(summary_item, dict) and summary_item.get("type") == "summary_text":
                        reasoning_content += summary_item.get("text", "")
                    elif isinstance(summary_item, str):
                        reasoning_content += summary_item
            elif output_item.get("type") == "message":
                # 提取最终文本输出
                for part in output_item.get("content", []):
                    if part.get("type") == "output_text":
                        content += part.get("text", "")
            elif output_item.get("type") == "function_call":
                # 解析工具调用，处理可能的参数格式错误
                parsed_arguments, invalid_tool_call = self._parse_tool_call_arguments(output_item)
                if invalid_tool_call:
                    invalid_tool_calls.append(invalid_tool_call)
                    continue

                tool_calls.append(
                    {
                        "name": output_item["name"],
                        "args": parsed_arguments or {},
                        "id": output_item.get("call_id", ""),
                        "type": "tool_call",
                    }
                )

        # 构建 usage_metadata 和 response_metadata
        usage = response.get("usage", {})
        usage_metadata = _build_usage_metadata(usage) if usage else None
        additional_kwargs = {}
        if reasoning_content:
            additional_kwargs["reasoning_content"] = reasoning_content

        message = AIMessage(
            content=content,
            tool_calls=tool_calls if tool_calls else [],
            invalid_tool_calls=invalid_tool_calls,
            additional_kwargs=additional_kwargs,
            usage_metadata=usage_metadata,
            response_metadata={
                "model": response.get("model", self.model),
                "usage": usage,
            },
        )

        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={
                "token_usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
                "model_name": response.get("model", self.model),
            },
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步生成回复。

        Args:
            messages: LangChain 消息列表。
            stop: 停止词列表（可选）。
            run_manager: 回调管理器（可选）。
            **kwargs: 其他关键字参数，可包含 tools 工具列表。

        Returns:
            ChatResult: LangChain 格式的聊天结果。
        """
        tools = kwargs.get("tools", None)
        response = self._call_codex_api(messages, tools=tools)
        return self._parse_response(response)

    def bind_tools(self, tools: list, **kwargs: Any) -> Any:
        """绑定工具列表，启用函数调用能力。

        将 LangChain 工具转换为 Codex Responses API 格式，并通过 RunnableBinding
        将其绑定到模型实例。支持 BaseTool 实例、OpenAI function 格式字典和
        扁平格式字典三种输入格式。

        Args:
            tools: 工具列表，支持 BaseTool 实例或字典。
            **kwargs: 其他传递给 RunnableBinding 的关键字参数。

        Returns:
            RunnableBinding: 绑定了工具的模型运行时。
        """
        from langchain_core.runnables import RunnableBinding
        from langchain_core.tools import BaseTool
        from langchain_core.utils.function_calling import convert_to_openai_function

        formatted_tools = []
        for tool in tools:
            if isinstance(tool, BaseTool):
                # BaseTool 实例：尝试转换为 OpenAI function 格式
                try:
                    fn = convert_to_openai_function(tool)
                    formatted_tools.append(
                        {
                            "type": "function",
                            "name": fn["name"],
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {}),
                        }
                    )
                except Exception:
                    # 转换失败时使用基本的工具描述
                    formatted_tools.append(
                        {
                            "type": "function",
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": {"type": "object", "properties": {}},
                        }
                    )
            elif isinstance(tool, dict):
                if "function" in tool:
                    # OpenAI function 格式（嵌套 function 对象）
                    fn = tool["function"]
                    formatted_tools.append(
                        {
                            "type": "function",
                            "name": fn["name"],
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {}),
                        }
                    )
                else:
                    # 已经是扁平格式，直接使用
                    formatted_tools.append(tool)

        return RunnableBinding(bound=self, kwargs={"tools": formatted_tools}, **kwargs)
