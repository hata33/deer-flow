"""华为昇腾 MindIE 推理引擎适配器。

模块功能
========
为华为昇腾（Ascend）NPU 平台上的 MindIE 推理引擎提供 LangChain 兼容的
聊天模型适配器。MindIE 通过 OpenAI 兼容的 API 暴露模型能力，但在消息格式、
工具调用和流式响应等方面存在兼容性问题，需要特殊处理。

核心设计
========
1. **消息格式修复**: MindIE 的 chat template 可能无法正确解析 LangChain 的
   原生 tool_calls 或 ToolMessage 角色，导致 0-token 生成错误。
   本模块通过将多模态列表内容扁平化为字符串、将工具相关消息转换为
   XML 标签格式来解决此问题。

2. **XML 工具调用解析**: MindIE 模型输出的工具调用使用硬编码的 XML 格式
   （<tool_call/function/parameter>），需要解析为 LangChain 标准的
   tool_calls 字典格式。

3. **流式兼容**: MindIE 在 stream=True 且存在工具调用时会丢失 choices，
   本模块通过回退到非流式生成并以模拟流式输出（分块 yield）来解决。

4. **转义修复**: 修复网关响应中过度转义的换行符（\\n -> \\n），
   同时保留代码块内的原始转义。

关键特性
========
- 多模态内容扁平化（list -> str）
- AIMessage 工具调用转 XML 文本
- ToolMessage 转 HumanMessage（XML 标签包裹）
- XML 工具调用结果解析（支持嵌套）
- 嵌套参数提取（排除内嵌 tool_call 块）
- 参数值自动反序列化（JSON/Python 字面量）
- 代码块外转义换行符修复
- 工具调用时的流式降级

使用场景
========
在华为昇腾 NPU 环境中使用 MindIE 推理引擎时::

    - name: qwen-72b
      use: deerflow.models.mindie_provider:MindIEChatModel
      model: Qwen/Qwen2.5-72B-Instruct
      base_url: http://mindie-server:1025/v1
      max_tokens: 8192

注意事项
========
- MindIE 的工具调用格式为硬编码的 XML，与 OpenAI 的 JSON 格式不同
- 工具调用结果被包装在 XML 标签中并转换为 HumanMessage
- 空消息内容会被替换为空格以避免 API 错误
"""

import ast
import html
import json
import re
import uuid
from collections.abc import Iterator

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


def _fix_messages(messages: list) -> list:
    """将消息列表转换为 MindIE 兼容格式。

    MindIE 的 chat template 可能无法正确处理 LangChain 的原生格式，
    因此需要：
    1. 将列表形式的多模态内容扁平化为纯文本字符串
    2. 将包含 tool_calls 的 AIMessage 转换为 XML 格式的文本
    3. 将 ToolMessage（工具执行结果）转换为 HumanMessage（XML 标签包裹）
    4. 空内容替换为空格以防止 API 错误

    Args:
        messages: LangChain 消息列表。

    Returns:
        list: 兼容 MindIE 的消息列表。
    """
    fixed = []
    for msg in messages:
        # 将列表形式的内容扁平化为纯文本
        if isinstance(msg.content, list):
            parts = []
            for block in msg.content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            text = "".join(parts)
        else:
            text = msg.content or ""

        # 将 AIMessage 的 tool_calls 转换为 XML 文本格式
        # MindIE 使用 <function=name><parameter=key>value</parameter> 的 XML 格式
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", []):
            xml_parts = []
            for tool in msg.tool_calls:
                args_xml = " ".join(f"<parameter={html.escape(str(k), quote=False)}>{html.escape(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), quote=False)}</parameter>" for k, v in tool.get("args", {}).items())
                xml_parts.append(f"```-snippet_claudeai_tool_call\n<function={html.escape(str(tool['name']), quote=False)}> {args_xml} </function>\n```\n")
            full_text = f"{text}\n" + "\n".join(xml_parts) if text else "\n".join(xml_parts)
            fixed.append(AIMessage(content=full_text.strip() or " "))
            continue

        # 将工具执行结果包装在 XML 标签中，并转换为 HumanMessage
        # MindIE 的 chat template 期望工具结果作为用户消息出现
        if isinstance(msg, ToolMessage):
            tool_result_text = f"```snippet_claudeai_tool_result\n{text}\n```\n"
            fixed.append(HumanMessage(content=tool_result_text))
            continue

        # 兜底处理：确保内容不为空，避免 API 拒绝请求
        if not text.strip():
            text = " "

        fixed.append(msg.model_copy(update={"content": text}))

    return fixed


def _parse_xml_tool_call_to_dict(content: str) -> tuple[str, list[dict]]:
    """将模型输出中的 XML 风格工具调用解析为 LangChain 标准字典格式。

    解析 <tool_call/function/parameter> 格式的 XML 块，提取函数名和参数，
    并转换为 LangChain 的 tool_calls 字典格式。同时清理原文中的 XML 块。

    参数值会尝试反序列化为原生 Python 类型（JSON/字面量），
    以满足下游 Pydantic 验证的要求。

    Args:
        content: 模型的原始文本输出。

    Returns:
        tuple[str, list[dict]]: 元组包含两个元素：
            - str: 清除 XML 块后的干净文本
            - list[dict]: LangChain 格式的工具调用列表，每个字典包含
              name、args 和 id 字段
    """
    if not isinstance(content, str) or "```-snippet_claudeai_tool_call" not in content:
        return content, []

    tool_calls = []
    clean_parts: list[str] = []
    cursor = 0
    for start, end, inner_content in _iter_tool_call_blocks(content):
        # 收集 XML 块之间的文本内容
        clean_parts.append(content[cursor:start])
        cursor = end

        # 提取函数名
        func_match = re.search(r"<function=([^>]+)>", inner_content)
        if not func_match:
            continue
        function_name = html.unescape(func_match.group(1).strip())

        # 提取参数时，需要排除内嵌的 tool_call 块
        # 内嵌的 ```-snippet_claudeai_tool_call 段代表独立的调用，其 <parameter>
        # 标签不应泄漏到当前调用的参数中
        param_source_parts: list[str] = []
        nested_cursor = 0
        for nested_start, nested_end, _ in _iter_tool_call_blocks(inner_content):
            param_source_parts.append(inner_content[nested_cursor:nested_start])
            nested_cursor = nested_end
        param_source_parts.append(inner_content[nested_cursor:])
        param_source = "".join(param_source_parts)

        # 解析 <parameter=key>value</parameter> 标签
        args = {}
        param_pattern = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)
        for param_match in param_pattern.finditer(param_source):
            key = html.unescape(param_match.group(1).strip())
            raw_value = html.unescape(param_match.group(2).strip())

            # 尝试将字符串值反序列化为原生 Python 类型
            # 以满足下游 Pydantic 验证的要求
            parsed_value = raw_value
            if raw_value.startswith(("[", "{")) or raw_value in ("true", "false", "null") or raw_value.isdigit():
                try:
                    parsed_value = json.loads(raw_value)
                except json.JSONDecodeError:
                    try:
                        parsed_value = ast.literal_eval(raw_value)
                    except (ValueError, SyntaxError):
                        pass

            args[key] = parsed_value

        # 生成唯一的工具调用 ID
        tool_calls.append({"name": function_name, "args": args, "id": f"call_{uuid.uuid4().hex[:10]}"})
    clean_parts.append(content[cursor:])

    return "".join(clean_parts).strip(), tool_calls


def _iter_tool_call_blocks(content: str) -> Iterator[tuple[int, int, str]]:
    """迭代文本中的 tool_call XML 块，支持嵌套容忍。

    遍历 ````-snippet_claudeai_tool_call ... ```` 块，支持嵌套（通过深度计数），
    避免过早闭合外层块。

    Args:
        content: 包含 tool_call 块的文本。

    Yields:
        tuple[int, int, str]: 每个块的 (起始位置, 结束位置, 内部内容)。
    """
    token_pattern = re.compile(r"</?```-snippet_claudeai_tool_call>")
    depth = 0
    block_start = -1

    for match in token_pattern.finditer(content):
        token = match.group(0)
        if token == "<```-snippet_claudeai_tool_call>":
            if depth == 0:
                block_start = match.start()
            depth += 1
            continue

        if depth == 0:
            continue

        depth -= 1
        if depth == 0 and block_start != -1:
            block_end = match.end()
            inner_start = block_start + len("<```-snippet_claudeai_tool_call>")
            inner_end = match.start()
            yield block_start, block_end, content[inner_start:inner_end]
            block_start = -1


def _decode_escaped_newlines_outside_fences(content: str) -> str:
    """解码代码块外的转义换行符。

    MindIE 网关响应中可能包含过度转义的换行符（字面的 \\n），
    此函数将其转换为真实的换行符，但保留 fenced code blocks（```）
    内部的原始转义。

    Args:
        content: 待处理的文本内容。

    Returns:
        str: 修复后的文本内容。
    """
    if "\\n" not in content:
        return content

    # 按代码块分隔符分割，仅处理非代码块部分
    parts = re.split(r"(```[\s\S]*?```)", content)
    for idx, part in enumerate(parts):
        if part.startswith("```"):
            # 跳过代码块内的内容
            continue
        parts[idx] = part.replace("\\n", "\n")
    return "".join(parts)


class MindIEChatModel(ChatOpenAI):
    """华为昇腾 MindIE 推理引擎的聊天模型适配器。

    继承自 ChatOpenAI，通过 MindIE 的 OpenAI 兼容 API 进行通信，
    并解决以下兼容性问题：
    - 将多模态列表内容扁平化为字符串
    - 拦截并解析硬编码的 XML 工具调用为 LangChain 标准格式
    - 处理 stream=True 且存在工具时丢失 choices 的问题
      （回退到非流式生成，模拟流式输出）
    - 修复网关响应中过度转义的换行符

    配置示例::

        - name: qwen-72b
          use: deerflow.models.mindie_provider:MindIEChatModel
          model: Qwen/Qwen2.5-72B-Instruct
          base_url: http://mindie-server:1025/v1
          max_tokens: 8192
    """

    def __init__(self, **kwargs):
        """初始化 MindIE 模型，规范化超时参数。

        将分散的超时参数（connect_timeout、read_timeout 等）合并为
        httpx.Timeout 对象，避免创建长期存活的客户端连接。

        Args:
            **kwargs: 传递给 ChatOpenAI 的关键字参数。
        """
        connect_timeout = kwargs.pop("connect_timeout", 30.0)
        read_timeout = kwargs.pop("read_timeout", 900.0)    # 推理可能需要较长时间
        write_timeout = kwargs.pop("write_timeout", 60.0)
        pool_timeout = kwargs.pop("pool_timeout", 30.0)

        kwargs.setdefault(
            "timeout",
            httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
        )
        super().__init__(**kwargs)

    def _patch_result_with_tools(self, result: ChatResult) -> ChatResult:
        """对模型生成结果进行后处理修复。

        处理流程：
        1. 修复消息内容中的过度转义换行符
        2. 检测并解析 XML 格式的工具调用
        3. 将解析结果合并到消息的 tool_calls 字段中

        Args:
            result: 原始的 ChatResult 对象。

        Returns:
            ChatResult: 修复后的 ChatResult 对象。
        """
        for gen in result.generations:
            msg = gen.message

            if isinstance(msg.content, str):
                # 保留代码块内的原始转义，仅修复外部内容
                msg.content = _decode_escaped_newlines_outside_fences(msg.content)

                # 检测 XML 工具调用标记并解析
                if "```-snippet_claudeai_tool_call" in msg.content:
                    clean_content, extracted_tools = _parse_xml_tool_call_to_dict(msg.content)

                    if extracted_tools:
                        msg.content = clean_content
                        if getattr(msg, "tool_calls", None) is None:
                            msg.tool_calls = []
                        msg.tool_calls.extend(extracted_tools)
        return result

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """同步生成回复。

        Args:
            messages: LangChain 消息列表。
            stop: 停止词列表（可选）。
            run_manager: 回调管理器（可选）。
            **kwargs: 其他关键字参数。

        Returns:
            ChatResult: 经过后处理的生成结果。
        """
        result = super()._generate(_fix_messages(messages), stop=stop, run_manager=run_manager, **kwargs)
        return self._patch_result_with_tools(result)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """异步生成回复。

        Args:
            messages: LangChain 消息列表。
            stop: 停止词列表（可选）。
            run_manager: 回调管理器（可选）。
            **kwargs: 其他关键字参数。

        Returns:
            ChatResult: 经过后处理的生成结果。
        """
        result = await super()._agenerate(_fix_messages(messages), stop=stop, run_manager=run_manager, **kwargs)
        return self._patch_result_with_tools(result)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        """异步流式生成回复。

        对于普通查询，使用原生流式输出以获得更低的 TTFB（首字节时间）。
        对于包含工具调用的查询，MindIE 当前在 stream=True 时会丢失 choices，
        因此回退到非流式生成，然后将结果分块 yield 以模拟流式输出。

        Args:
            messages: LangChain 消息列表。
            stop: 停止词列表（可选）。
            run_manager: 回调管理器（可选）。
            **kwargs: 其他关键字参数。

        Yields:
            ChatGenerationChunk: 生成内容块。
        """
        # 无工具调用时使用原生流式输出
        if not kwargs.get("tools"):
            async for chunk in super()._astream(_fix_messages(messages), stop=stop, run_manager=run_manager, **kwargs):
                if isinstance(chunk.message.content, str):
                    chunk.message.content = _decode_escaped_newlines_outside_fences(chunk.message.content)
                yield chunk
            return

        # 工具调用场景的降级处理：
        # MindIE 在 stream=True 且有工具时会丢失 choices，
        # 因此先完整生成，再分块 yield 模拟流式输出
        result = await self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

        for gen in result.generations:
            msg = gen.message
            content = msg.content
            standard_tool_calls = getattr(msg, "tool_calls", [])

            # 分块 yield 文本内容，让下游 UI/Markdown 渲染器平滑显示
            if isinstance(content, str) and content:
                chunk_size = 15
                for i in range(0, len(content), chunk_size):
                    chunk_text = content[i : i + chunk_size]
                    # 仅第一个块携带 response_metadata，避免重复发送
                    chunk_msg = AIMessageChunk(content=chunk_text, id=msg.id, response_metadata=msg.response_metadata if i == 0 else {})
                    yield ChatGenerationChunk(message=chunk_msg, generation_info=gen.generation_info if i == 0 else None)

                # 工具调用信息在文本块之后单独发送
                if standard_tool_calls:
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", id=msg.id, tool_calls=standard_tool_calls, invalid_tool_calls=getattr(msg, "invalid_tool_calls", [])))
            else:
                # 纯工具调用（无文本内容）
                chunk_msg = AIMessageChunk(content=content, id=msg.id, tool_calls=standard_tool_calls, invalid_tool_calls=getattr(msg, "invalid_tool_calls", []))
                yield ChatGenerationChunk(message=chunk_msg, generation_info=gen.generation_info)
