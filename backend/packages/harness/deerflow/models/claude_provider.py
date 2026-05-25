"""自定义 Claude 提供商 — 支持 OAuth Bearer 认证、提示缓存和智能思维预算。

模块功能
========
基于 LangChain 的 `ChatAnthropic` 进行扩展，为 Anthropic Claude 模型提供
增强功能，主要包括：
- **双认证模式**: 同时支持标准 API Key 认证和 Claude Code OAuth Token 认证
- **提示缓存**: 自动在系统提示、最近消息和工具定义上添加缓存断点，
  减少重复 Token 消耗
- **智能思维预算**: 根据模型的 `max_tokens` 自动计算并分配思维预算
- **OAuth 计费头注入**: 自动注入 Anthropic API 要求的计费元数据
- **重试与退避**: 对速率限制和服务端错误实现指数退避重试

核心设计
========
1. **OAuth Token 检测**: 通过 `sk-ant-oat` 前缀自动识别 OAuth Token，
   无需用户手动指定认证方式
2. **客户端补丁**: 在 SDK 客户端创建后将 `api_key` 替换为 `auth_token`，
   实现 Bearer 认证而非默认的 x-api-key 认证
3. **缓存策略**: 采用"最后 N 个候选块"策略放置缓存断点，
   因为靠后的断点覆盖更大的前缀，缓存命中率更高
4. **计费头处理**: OAuth Token 访问要求计费头必须是系统提示的第一个块，
   模块会自动移除重复项并确保位置正确

认证来源优先级
==============
1. $ANTHROPIC_API_KEY 环境变量
2. $CLAUDE_CODE_OAUTH_TOKEN 或 $ANTHROPIC_AUTH_TOKEN
3. $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR（文件描述符）
4. $CLAUDE_CODE_CREDENTIALS_PATH 指定的凭证文件
5. ~/.claude/.credentials.json 默认凭证文件

使用场景
========
在 `config.yaml` 中配置 Claude 模型时使用::

    - name: claude-sonnet-4.6
      use: deerflow.models.claude_provider:ClaudeChatModel
      model: claude-sonnet-4-6
      max_tokens: 16384
      enable_prompt_caching: true

注意事项
========
- OAuth Token 最多支持 4 个 cache_control 块，启用 OAuth 时会自动禁用提示缓存
- 计费头格式需与 Claude Code CLI 保持一致，可通过环境变量覆盖
- 重试策略仅针对 RateLimitError 和 InternalServerError，其他异常直接抛出
"""

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from typing import Any

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)

# 重试次数上限：针对速率限制和服务端错误的最大重试次数
MAX_RETRIES = 3

# 思维预算比例：自动将 max_tokens 的 80% 分配给思维预算
# 留出 20% 给最终输出，确保模型有足够的输出空间
THINKING_BUDGET_RATIO = 0.8

# Anthropic API 要求的计费头，用于 OAuth Token 访问时的计费验证。
# 必须作为系统提示的第一个文本块。格式与 Claude Code CLI 保持一致。
# 如果硬编码版本过期，可通过 ANTHROPIC_BILLING_HEADER 环境变量覆盖。
_DEFAULT_BILLING_HEADER = "x-anthropic-billing-header: cc_version=2.1.85.351; cc_entrypoint=cli; cch=6c6d5;"
OAUTH_BILLING_HEADER = os.environ.get("ANTHROPIC_BILLING_HEADER", _DEFAULT_BILLING_HEADER)


class ClaudeChatModel(ChatAnthropic):
    """扩展的 ChatAnthropic 模型，支持 OAuth Bearer 认证、提示缓存和智能思维预算。

    本类在 LangChain 的 ChatAnthropic 基础上增加了以下能力：
    - OAuth Token 自动检测与 Bearer 认证切换
    - 基于缓存断点预算的提示缓存自动应用
    - 根据模型 max_tokens 自动计算思维预算
    - 针对速率限制和服务端错误的指数退避重试

    Attributes:
        enable_prompt_caching: 是否启用提示缓存。OAuth Token 会自动禁用此选项，
            因为 OAuth Token 最多只支持 4 个 cache_control 块。
        prompt_cache_size: 最近 N 条消息参与缓存候选。默认为 3。
        auto_thinking_budget: 是否自动计算思维预算。默认为 True。
        retry_max_attempts: 最大重试次数。默认为 3。

    使用示例::

        - name: claude-sonnet-4.6
          use: deerflow.models.claude_provider:ClaudeChatModel
          model: claude-sonnet-4-6
          max_tokens: 16384
          enable_prompt_caching: true
    """

    # ---- 自定义配置字段 ----
    enable_prompt_caching: bool = True       # 是否自动添加 cache_control 断点
    prompt_cache_size: int = 3               # 参与缓存候选的最近消息数
    auto_thinking_budget: bool = True        # 是否自动分配思维预算
    retry_max_attempts: int = MAX_RETRIES    # 重试上限

    # OAuth 状态（私有属性，不参与序列化）
    _is_oauth: bool = PrivateAttr(default=False)           # 当前是否使用 OAuth Token
    _oauth_access_token: str = PrivateAttr(default="")     # OAuth 访问令牌

    model_config = {"arbitrary_types_allowed": True}

    def _validate_retry_config(self) -> None:
        """验证重试配置的合法性。

        确保重试次数至少为 1，避免配置错误导致无限循环或无法重试。

        Raises:
            ValueError: 当 retry_max_attempts < 1 时抛出。
        """
        if self.retry_max_attempts < 1:
            raise ValueError("retry_max_attempts must be >= 1")

    def model_post_init(self, __context: Any) -> None:
        """模型初始化后处理：自动加载凭证并配置 OAuth 认证。

        执行流程：
        1. 提取当前 API Key 的明文值
        2. 如果没有有效 Key，尝试从 Claude Code CLI 凭证源加载
        3. 检测是否为 OAuth Token，若是则切换为 Bearer 认证模式
        4. 调用父类初始化创建 SDK 客户端
        5. 对 OAuth 模式的客户端进行认证方式补丁

        Args:
            __context: Pydantic 模型初始化上下文（由框架传入）。
        """
        from pydantic import SecretStr

        from deerflow.models.credential_loader import (
            OAUTH_ANTHROPIC_BETAS,
            is_oauth_token,
            load_claude_code_credential,
        )

        self._validate_retry_config()

        # 提取 API Key 的明文值（SecretStr 的 str() 方法返回星号掩码）
        current_key = ""
        if self.anthropic_api_key:
            if hasattr(self.anthropic_api_key, "get_secret_value"):
                current_key = self.anthropic_api_key.get_secret_value()
            else:
                current_key = str(self.anthropic_api_key)

        # 如果没有有效的 API Key，尝试从 Claude Code CLI 的各种凭证源加载
        if not current_key or current_key in ("your-anthropic-api-key",):
            cred = load_claude_code_credential()
            if cred:
                current_key = cred.access_token
                logger.info(f"Using Claude Code CLI credential (source: {cred.source})")
            else:
                logger.warning("No Anthropic API key or explicit Claude Code OAuth credential found.")

        # 检测到 OAuth Token 后，切换为 Bearer 认证模式
        if is_oauth_token(current_key):
            self._is_oauth = True
            self._oauth_access_token = current_key
            # 临时设置 token 为 api_key（后续会在客户端层面替换为 auth_token）
            self.anthropic_api_key = SecretStr(current_key)
            # 添加 OAuth 所需的 beta 头信息
            self.default_headers = {
                **(self.default_headers or {}),
                "anthropic-beta": OAUTH_ANTHROPIC_BETAS,
            }
            # OAuth Token 最多只支持 4 个 cache_control 块，因此禁用提示缓存
            self.enable_prompt_caching = False
            logger.info("OAuth token detected — will use Authorization: Bearer header")
        else:
            # 标准 API Key 模式
            if current_key:
                self.anthropic_api_key = SecretStr(current_key)

        # 确保 api_key 始终为 SecretStr 类型
        if isinstance(self.anthropic_api_key, str):
            self.anthropic_api_key = SecretStr(self.anthropic_api_key)

        # 调用父类初始化，创建 Anthropic SDK 客户端
        super().model_post_init(__context)

        # 在客户端创建后立即进行 OAuth 认证补丁
        # 必须在 super() 之后执行，因为客户端是延迟创建的
        if self._is_oauth:
            self._patch_client_oauth(self._client)
            self._patch_client_oauth(self._async_client)

    def _patch_client_oauth(self, client: Any) -> None:
        """将 Anthropic SDK 客户端的认证方式从 api_key 切换为 auth_token。

        OAuth Token 需要通过 `Authorization: Bearer` 头发送，而非默认的
        `x-api-key` 头。此方法通过将 `api_key` 设为 None 并设置 `auth_token`
        来实现认证方式的切换。

        Args:
            client: Anthropic SDK 客户端实例（同步或异步）。
        """
        if hasattr(client, "api_key") and hasattr(client, "auth_token"):
            client.api_key = None
            client.auth_token = self._oauth_access_token

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """重写请求负载构建：注入提示缓存、思维预算和 OAuth 计费信息。

        在父类生成的标准负载基础上，按需添加以下增强：
        1. OAuth 计费头（OAuth 模式下必须）
        2. 提示缓存断点（降低重复 Token 消耗）
        3. 自动思维预算分配

        Args:
            input_: LangChain 消息输入（消息列表或提示模板）。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            dict: 增强后的 API 请求负载字典。
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # OAuth 模式下注入计费头（Anthropic API 要求）
        if self._is_oauth:
            self._apply_oauth_billing(payload)

        # 应用提示缓存断点
        if self.enable_prompt_caching:
            self._apply_prompt_caching(payload)

        # 自动计算思维预算
        if self.auto_thinking_budget:
            self._apply_thinking_budget(payload)

        return payload

    def _apply_oauth_billing(self, payload: dict) -> None:
        """注入 OAuth 请求必需的计费头块。

        计费块必须始终放置在 system 列表的最前面。本方法会：
        1. 移除已存在的计费块以避免重复或顺序错误
        2. 在索引 0 的位置插入新的计费块
        3. 添加 API 要求的 metadata.user_id 字段

        Args:
            payload: 待发送的 API 请求负载。会被就地修改。
        """
        billing_block = {"type": "text", "text": OAUTH_BILLING_HEADER}

        system = payload.get("system")
        if isinstance(system, list):
            # 移除已有的计费块，然后在头部插入新的，确保位置正确
            filtered = [b for b in system if not (isinstance(b, dict) and OAUTH_BILLING_HEADER in b.get("text", ""))]
            payload["system"] = [billing_block] + filtered
        elif isinstance(system, str):
            if OAUTH_BILLING_HEADER in system:
                payload["system"] = [billing_block]
            else:
                payload["system"] = [billing_block, {"type": "text", "text": system}]
        else:
            payload["system"] = [billing_block]

        # 添加 OAuth 计费验证所需的 metadata.user_id
        if not isinstance(payload.get("metadata"), dict):
            payload["metadata"] = {}
        if "user_id" not in payload["metadata"]:
            # 基于主机名生成稳定的 device_id，确保同一设备的会话一致性
            hostname = socket.gethostname()
            device_id = hashlib.sha256(f"deerflow-{hostname}".encode()).hexdigest()
            session_id = str(uuid.uuid4())
            payload["metadata"]["user_id"] = json.dumps(
                {
                    "device_id": device_id,
                    "account_uuid": "deerflow",
                    "session_id": session_id,
                }
            )

    def _apply_prompt_caching(self, payload: dict) -> None:
        """在系统提示、最近消息和工具定义上应用临时缓存控制。

        缓存策略说明：
        - 使用固定预算 MAX_CACHE_BREAKPOINTS (4) 个断点，这是 Anthropic API 和
          AWS Bedrock 共同执行的硬限制
        - 断点放置在**最后**的候选块上，因为靠后的断点覆盖更大的前缀，
          缓存命中率更高
        - 系统提示被认为是完全静态的（无每用户记忆或当前日期）
        - 动态上下文通过 DynamicContextMiddleware 以 <system-reminder> 的形式
          注入到第一条 HumanMessage 中

        候选块收集顺序：
        1. 系统提示的文本块
        2. 最近 prompt_cache_size 条消息的内容块
        3. 最后一个工具定义

        Args:
            payload: 待发送的 API 请求负载。会被就地修改。
        """
        MAX_CACHE_BREAKPOINTS = 4

        # 按文档顺序收集候选块
        candidates: list[dict] = []

        # 1. 系统提示块
        system = payload.get("system")
        if system and isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    candidates.append(block)
        elif system and isinstance(system, str):
            # 字符串形式的系统提示需转换为列表格式
            new_block: dict = {"type": "text", "text": system}
            payload["system"] = [new_block]
            candidates.append(new_block)

        # 2. 最近消息的内容块
        messages = payload.get("messages", [])
        cache_start = max(0, len(messages) - self.prompt_cache_size)
        for i in range(cache_start, len(messages)):
            msg = messages[i]
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        candidates.append(block)
            elif isinstance(content, str) and content:
                # 字符串内容转换为列表格式以支持 cache_control 标记
                new_block = {"type": "text", "text": content}
                msg["content"] = [new_block]
                candidates.append(new_block)

        # 3. 最后一个工具定义（工具列表中最末尾的工具）
        tools = payload.get("tools", [])
        if tools and isinstance(tools[-1], dict):
            candidates.append(tools[-1])

        # 仅对最后 MAX_CACHE_BREAKPOINTS 个候选块应用缓存控制，保持在 API 限制内
        for block in candidates[-MAX_CACHE_BREAKPOINTS:]:
            block["cache_control"] = {"type": "ephemeral"}

    def _apply_thinking_budget(self, payload: dict) -> None:
        """根据 max_tokens 自动计算思维预算。

        当思维模式已启用但未设置 budget_tokens 时，自动将 max_tokens 的
        THINKING_BUDGET_RATIO (80%) 分配给思维预算，留出 20% 给最终输出。

        Args:
            payload: 待发送的 API 请求负载。会被就地修改。
        """
        thinking = payload.get("thinking")
        if not thinking or not isinstance(thinking, dict):
            return
        if thinking.get("type") != "enabled":
            return
        # 如果用户已手动设置预算，则不覆盖
        if thinking.get("budget_tokens"):
            return

        max_tokens = payload.get("max_tokens", 8192)
        thinking["budget_tokens"] = int(max_tokens * THINKING_BUDGET_RATIO)

    @staticmethod
    def _strip_cache_control(payload: dict) -> None:
        """在 OAuth 请求发送前移除 cache_control 标记。

        OAuth Token 访问时，Anthropic API 对 cache_control 块有严格限制（最多 4 个），
        为避免超限错误，在 _create/_acreate 调用前统一清理所有缓存标记。

        Args:
            payload: 待发送的 API 请求负载。会被就地修改。
        """
        for section in ("system", "messages"):
            items = payload.get(section)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item.pop("cache_control", None)
                content = item.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)

        tools = payload.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict):
                    tool.pop("cache_control", None)

    def _create(self, payload: dict) -> Any:
        """同步创建 API 调用，OAuth 模式下移除缓存标记。

        Args:
            payload: API 请求负载。

        Returns:
            Anthropic API 响应对象。
        """
        if self._is_oauth:
            self._strip_cache_control(payload)
        return super()._create(payload)

    async def _acreate(self, payload: dict) -> Any:
        """异步创建 API 调用，OAuth 模式下移除缓存标记。

        Args:
            payload: API 请求负载。

        Returns:
            Anthropic API 响应对象。
        """
        if self._is_oauth:
            self._strip_cache_control(payload)
        return await super()._acreate(payload)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, **kwargs: Any) -> Any:
        """同步生成回复，包含 OAuth 客户端补丁和重试逻辑。

        重试策略：
        - 仅对 RateLimitError（429）和 InternalServerError（500/529）进行重试
        - 采用指数退避算法（基础间隔 × 2^(attempt-1)）
        - 退避间隔增加 20% 的抖动以避免惊群效应
        - 如果服务端返回 Retry-After 头，则优先使用其指定的等待时间

        Args:
            messages: LangChain 消息列表。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            ChatResult: 聊天生成结果。

        Raises:
            anthropic.RateLimitError: 超过重试上限后的速率限制错误。
            anthropic.InternalServerError: 超过重试上限后的服务端错误。
        """
        # 每次生成前重新应用 OAuth 补丁（防御性措施）
        if self._is_oauth:
            self._patch_client_oauth(self._client)

        last_error = None
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                return super()._generate(messages, stop=stop, **kwargs)
            except anthropic.RateLimitError as e:
                last_error = e
                if attempt >= self.retry_max_attempts:
                    raise
                wait_ms = self._calc_backoff_ms(attempt, e)
                logger.warning(f"Rate limited, retrying attempt {attempt}/{self.retry_max_attempts} after {wait_ms}ms")
                time.sleep(wait_ms / 1000)
            except anthropic.InternalServerError as e:
                last_error = e
                if attempt >= self.retry_max_attempts:
                    raise
                wait_ms = self._calc_backoff_ms(attempt, e)
                logger.warning(f"Server error, retrying attempt {attempt}/{self.retry_max_attempts} after {wait_ms}ms")
                time.sleep(wait_ms / 1000)
        raise last_error

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, **kwargs: Any) -> Any:
        """异步生成回复，包含 OAuth 客户端补丁和重试逻辑。

        与同步版本 `_generate` 使用相同的重试策略，但使用 asyncio.sleep
        替代 time.sleep 以避免阻塞事件循环。

        Args:
            messages: LangChain 消息列表。
            stop: 停止词列表（可选）。
            **kwargs: 其他传递给父类的关键字参数。

        Returns:
            ChatResult: 聊天生成结果。

        Raises:
            anthropic.RateLimitError: 超过重试上限后的速率限制错误。
            anthropic.InternalServerError: 超过重试上限后的服务端错误。
        """
        import asyncio

        # 每次生成前重新应用 OAuth 补丁（防御性措施）
        if self._is_oauth:
            self._patch_client_oauth(self._async_client)

        last_error = None
        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                return await super()._agenerate(messages, stop=stop, **kwargs)
            except anthropic.RateLimitError as e:
                last_error = e
                if attempt >= self.retry_max_attempts:
                    raise
                wait_ms = self._calc_backoff_ms(attempt, e)
                logger.warning(f"Rate limited, retrying attempt {attempt}/{self.retry_max_attempts} after {wait_ms}ms")
                await asyncio.sleep(wait_ms / 1000)
            except anthropic.InternalServerError as e:
                last_error = e
                if attempt >= self.retry_max_attempts:
                    raise
                wait_ms = self._calc_backoff_ms(attempt, e)
                logger.warning(f"Server error, retrying attempt {attempt}/{self.retry_max_attempts} after {wait_ms}ms")
                await asyncio.sleep(wait_ms / 1000)
        raise last_error

    @staticmethod
    def _calc_backoff_ms(attempt: int, error: Exception) -> int:
        """计算指数退避等待时间（毫秒）。

        退避策略：
        - 基础间隔：2000ms
        - 指数增长：2000 × 2^(attempt-1)
        - 固定抖动：增加 20% 缓冲以避免多个客户端同时重试
        - 服务端提示：如果错误响应包含 Retry-After 头，优先使用其值

        Args:
            attempt: 当前重试次数（从 1 开始）。
            error: 触发重试的异常对象。

        Returns:
            int: 等待时间（毫秒）。
        """
        backoff_ms = 2000 * (1 << (attempt - 1))
        jitter_ms = int(backoff_ms * 0.2)
        total_ms = backoff_ms + jitter_ms

        # 优先使用服务端指定的 Retry-After 时间
        if hasattr(error, "response") and error.response is not None:
            retry_after = error.response.headers.get("Retry-After")
            if retry_after:
                try:
                    total_ms = int(retry_after) * 1000
                except (ValueError, TypeError):
                    pass

        return total_ms
