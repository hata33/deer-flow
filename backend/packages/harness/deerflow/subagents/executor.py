"""子代理执行引擎模块。

本模块实现了子代理任务的核心执行引擎，包括:
- SubagentExecutor: 子代理执行器，负责创建 LangChain Agent 并在独立上下文中运行
- SubagentResult: 执行结果数据类，通过线程安全的状态锁管理终态转换
- 双线程池架构: _scheduler_pool 负责任务调度，持久化事件循环负责异步代理执行
- 后台任务管理: execute_async() 提交到线程池后通过全局字典跟踪状态

执行流程:
    task() 工具调用 → SubagentExecutor.execute_async() → _scheduler_pool 提交任务
    → 持久化事件循环中运行 _aexecute() → agent.astream() 流式执行
    → task_tool 以 5 秒间隔轮询 get_background_task_result() → SSE 事件发射
    → 终态（COMPLETED/FAILED/TIMED_OUT）→ 清理后台任务

并发控制:
    MAX_CONCURRENT_SUBAGENTS = 3 由 SubagentLimitMiddleware 在 after_model 阶段
    截断多余的 task 工具调用，确保同时运行的子代理不超过上限。

超时处理:
    默认 15 分钟超时。通过 Future.result(timeout=) 和 threading.Event.wait(timeout)
    实现。超时后设置 cancel_event 通知协作式取消，并将状态标记为 TIMED_OUT。

事件模型:
    task_started → task_running → task_completed / task_failed / task_timed_out
    通过 StreamWriter 以 SSE 格式推送给前端。
"""

import asyncio
import atexit
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from deerflow.agents.thread_state import SandboxState, ThreadDataState, ThreadState
from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.models import create_chat_model
from deerflow.skills.tool_policy import filter_tools_by_skill_allowed_tools
from deerflow.skills.types import Skill
from deerflow.subagents.config import SubagentConfig, resolve_subagent_model_name
from deerflow.subagents.token_collector import SubagentTokenCollector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块重载时清理前一个持久化事件循环
# 防止热重载场景下重复注册 atexit 回调和泄露旧循环
# ---------------------------------------------------------------------------
_previous_shutdown_isolated_subagent_loop = globals().get("_shutdown_isolated_subagent_loop")
if callable(_previous_shutdown_isolated_subagent_loop):
    atexit.unregister(_previous_shutdown_isolated_subagent_loop)
    _previous_shutdown_isolated_subagent_loop()


class SubagentStatus(Enum):
    """子代理执行状态枚举。

    状态流转:
        PENDING → RUNNING → COMPLETED（正常完成）
        PENDING → RUNNING → FAILED（执行异常）
        PENDING → RUNNING → TIMED_OUT（超时）
        PENDING → RUNNING → CANCELLED（用户取消）

    终态（terminal）状态包括 COMPLETED、FAILED、TIMED_OUT、CANCELLED，
    一旦进入终态不可逆转。try_set_terminal() 通过线程锁保证原子性。
    """

    PENDING = "pending"      # 已提交，等待执行
    RUNNING = "running"      # 正在执行中
    COMPLETED = "completed"  # 正常完成
    FAILED = "failed"        # 执行失败（异常）
    CANCELLED = "cancelled"  # 被用户或父代理取消
    TIMED_OUT = "timed_out"  # 执行超时

    @property
    def is_terminal(self) -> bool:
        """判断当前状态是否为终态。终态不可被后续状态转换覆盖。"""
        return self in {
            type(self).COMPLETED,
            type(self).FAILED,
            type(self).CANCELLED,
            type(self).TIMED_OUT,
        }


@dataclass
class SubagentResult:
    """子代理执行结果数据类。

    每个子代理执行（无论同步还是异步）都会产生一个 SubagentResult 实例。
    对于后台异步任务，结果存储在全局 _background_tasks 字典中，
    由 task_tool 通过 get_background_task_result() 轮询获取。

    线程安全保证:
        - _state_lock: 保护 try_set_terminal() 中的状态转换，防止超时线程
          和执行线程之间的竞态条件
        - cancel_event: 协作式取消信号，通过 threading.Event 实现
        - _background_tasks_lock: 保护全局 _background_tasks 字典的并发访问

    Attributes:
        task_id: 本次执行的唯一标识符（UUID 前 8 位）。
        trace_id: 分布式追踪 ID，关联父代理和子代理的日志。
        status: 当前执行状态。
        result: 最终结果消息（仅 COMPLETED 状态有值）。
        error: 错误消息（仅 FAILED/TIMED_OUT/CANCELLED 状态有值）。
        started_at: 执行开始时间。
        completed_at: 执行完成时间。
        ai_messages: 执行过程中生成的 AI 消息列表（序列化为字典）。
        token_usage_records: LLM 调用的 token 用量记录列表。
        usage_reported: token 用量是否已上报到父代理的 RunJournal。
        cancel_event: 协作式取消信号，由 request_cancel_background_task() 设置。
    """

    task_id: str
    trace_id: str
    status: SubagentStatus
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    ai_messages: list[dict[str, Any]] | None = None
    token_usage_records: list[dict[str, int | str]] = field(default_factory=list)
    usage_reported: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        """初始化可变默认值。确保 ai_messages 列表始终非 None。"""
        if self.ai_messages is None:
            self.ai_messages = []

    def try_set_terminal(
        self,
        status: SubagentStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        completed_at: datetime | None = None,
        ai_messages: list[dict[str, Any]] | None = None,
        token_usage_records: list[dict[str, int | str]] | None = None,
    ) -> bool:
        """原子性地将状态设置为终态。

        后台超时/取消线程和执行工作线程可能竞态地操作同一个 result holder。
        第一个成功的终态转换生效，后续的终态写入被拒绝，确保状态和载荷字段
        不会被后续写入覆盖。

        Args:
            status: 目标终态（必须是 COMPLETED/FAILED/CANCELLED/TIMED_OUT 之一）。
            result: 最终结果消息（可选）。
            error: 错误消息（可选）。
            completed_at: 完成时间（可选，默认使用 datetime.now()）。
            ai_messages: AI 消息列表（可选）。
            token_usage_records: token 用量记录列表（可选）。

        Returns:
            True 表示状态转换成功，False 表示已被其他线程抢先设置为终态。

        Raises:
            ValueError: 当传入的 status 不是终态时抛出。
        """
        if not status.is_terminal:
            raise ValueError(f"Status {status} is not terminal")

        with self._state_lock:
            if self.status.is_terminal:
                return False

            if result is not None:
                self.result = result
            if error is not None:
                self.error = error
            if ai_messages is not None:
                self.ai_messages = ai_messages
            if token_usage_records is not None:
                self.token_usage_records = token_usage_records
            self.completed_at = completed_at or datetime.now()
            self.status = status
            return True


# ---------------------------------------------------------------------------
# 全局状态：后台任务存储、线程池、持久化事件循环
# ---------------------------------------------------------------------------

# 后台任务结果的全局存储，task_id → SubagentResult 映射
_background_tasks: dict[str, SubagentResult] = {}

# 保护 _background_tasks 字典的线程锁
_background_tasks_lock = threading.Lock()

# 调度线程池：负责将后台任务编排提交到持久化事件循环
# 3 个工作线程对应 MAX_CONCURRENT_SUBAGENTS = 3 的并发上限
_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-scheduler-")

# 持久化事件循环：用于从已运行的事件循环中触发隔离的子代理执行
# 复用一个长生命周期的循环，避免为每次执行创建临时循环并关闭绑定其上的异步资源
# （如 httpx 客户端连接池）
_isolated_subagent_loop: asyncio.AbstractEventLoop | None = None
_isolated_subagent_loop_thread: threading.Thread | None = None
_isolated_subagent_loop_started: threading.Event | None = None
_isolated_subagent_loop_lock = threading.Lock()


def _run_isolated_subagent_loop(
    loop: asyncio.AbstractEventLoop,
    started_event: threading.Event,
) -> None:
    """在专用守护线程中运行持久化事件循环。

    该循环在整个进程生命周期内持续运行，接收通过
    asyncio.run_coroutine_threadsafe() 提交的子代理协程。
    started_event 在循环开始运行后设置，供创建者同步等待。

    Args:
        loop: 要运行的 asyncio 事件循环。
        started_event: 启动完成信号，循环开始后立即 set。
    """
    asyncio.set_event_loop(loop)
    loop.call_soon(started_event.set)
    try:
        loop.run_forever()
    finally:
        started_event.clear()


def _shutdown_isolated_subagent_loop() -> None:
    """停止并关闭持久化事件循环。

    在进程退出时由 atexit 触发，确保异步资源（httpx 连接池等）
    被正确清理。如果关闭超时，仅记录警告而不强制关闭，避免
    破坏仍在使用循环的资源。
    """
    global _isolated_subagent_loop, _isolated_subagent_loop_thread, _isolated_subagent_loop_started

    with _isolated_subagent_loop_lock:
        loop = _isolated_subagent_loop
        thread = _isolated_subagent_loop_thread
        _isolated_subagent_loop = None
        _isolated_subagent_loop_thread = None
        _isolated_subagent_loop_started = None

    if loop is None:
        return

    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1)

    thread_stopped = thread is None or not thread.is_alive()
    loop_stopped = not loop.is_running()

    if not loop.is_closed():
        if thread_stopped and loop_stopped:
            loop.close()
        else:
            logger.warning(
                "Skipping close of isolated subagent loop because shutdown did not complete within timeout (thread_alive=%s, loop_running=%s)",
                thread is not None and thread.is_alive(),
                loop.is_running(),
            )


atexit.register(_shutdown_isolated_subagent_loop)


def _get_isolated_subagent_loop() -> asyncio.AbstractEventLoop:
    """获取或创建持久化事件循环。

    首次调用时创建一个新的 asyncio 事件循环和守护线程。
    后续调用复用已有的循环。如果检测到循环不可用（线程退出、循环关闭），
    自动重建。

    Returns:
        可用的持久化 asyncio 事件循环。

    Raises:
        RuntimeError: 如果事件循环启动超时（5 秒）或初始化失败。
    """
    global _isolated_subagent_loop, _isolated_subagent_loop_thread, _isolated_subagent_loop_started
    with _isolated_subagent_loop_lock:
        thread_is_alive = _isolated_subagent_loop_thread is not None and _isolated_subagent_loop_thread.is_alive()
        loop_is_usable = _isolated_subagent_loop is not None and not _isolated_subagent_loop.is_closed() and _isolated_subagent_loop.is_running() and thread_is_alive

        if not loop_is_usable:
            loop = asyncio.new_event_loop()
            started_event = threading.Event()
            thread = threading.Thread(
                target=_run_isolated_subagent_loop,
                args=(loop, started_event),
                name="subagent-persistent-loop",
                daemon=True,
            )
            thread.start()
            if not started_event.wait(timeout=5):
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1)
                loop.close()
                raise RuntimeError("Timed out starting isolated subagent event loop")
            _isolated_subagent_loop = loop
            _isolated_subagent_loop_thread = thread
            _isolated_subagent_loop_started = started_event

        if _isolated_subagent_loop is None:
            raise RuntimeError("Isolated subagent event loop is not initialized")
        return _isolated_subagent_loop


def _submit_to_isolated_loop_in_context(
    context: Context,
    coro_factory: Callable[[], Coroutine[Any, Any, SubagentResult]],
) -> Future[SubagentResult]:
    """将协程提交到持久化事件循环，同时保留 ContextVar 状态。

    使用 contextvars.copy_context() 捕获当前线程的上下文变量
    （如 user_id、trace_id 等），在持久化循环的线程中恢复这些变量，
    确保子代理执行时能正确访问父代理的上下文信息。

    Args:
        context: 通过 copy_context() 获取的上下文快照。
        coro_factory: 返回子代理执行协程的可调用对象。

    Returns:
        concurrent.futures.Future，可用于等待执行结果或取消。
    """
    return context.run(
        lambda: asyncio.run_coroutine_threadsafe(
            coro_factory(),
            _get_isolated_subagent_loop(),
        )
    )


def _filter_tools(
    all_tools: list[BaseTool],
    allowed: list[str] | None,
    disallowed: list[str] | None,
) -> list[BaseTool]:
    """根据子代理配置过滤可用工具列表。

    过滤逻辑:
    1. 如果指定了 allowed 白名单，仅保留白名单中的工具
    2. 从结果中移除 disallowed 黑名单中的工具
    3. 两个过滤步骤按顺序执行，黑名单优先级高于白名单

    Args:
        all_tools: 父代理的全部可用工具列表。
        allowed: 工具名称白名单。None 表示不限制。
        disallowed: 工具名称黑名单。None 表示不过滤。

    Returns:
        过滤后的工具列表。
    """
    filtered = all_tools

    # 应用白名单过滤
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]

    # 应用黑名单过滤
    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [t for t in filtered if t.name not in disallowed_set]

    return filtered


class SubagentExecutor:
    """子代理执行器。

    负责根据 SubagentConfig 创建 LangChain Agent 实例并在隔离上下文中执行任务。
    支持两种执行模式:
    - execute(): 同步执行，阻塞调用线程直到任务完成或超时
    - execute_async(): 异步执行，提交到后台线程池后立即返回 task_id

    执行路径选择:
    - 当调用方已在事件循环中时 → _execute_in_isolated_loop()（持久化循环）
    - 当调用方不在事件循环中时 → asyncio.run()（新建临时循环）
    - execute_async() 始终通过 _scheduler_pool → 持久化循环

    工具与技能加载:
    1. _filter_tools() 根据配置过滤工具
    2. _load_skills() 加载并过滤技能
    3. _apply_skill_allowed_tools() 根据技能元数据进一步限制工具
    4. _load_skill_messages() 将技能内容注入为 SystemMessage
    """

    def __init__(
        self,
        config: SubagentConfig,
        tools: list[BaseTool],
        app_config: AppConfig | None = None,
        parent_model: str | None = None,
        sandbox_state: SandboxState | None = None,
        thread_data: ThreadDataState | None = None,
        thread_id: str | None = None,
        trace_id: str | None = None,
    ):
        """初始化执行器。

        在初始化阶段完成工具过滤和模型名称解析（部分场景延迟到 _create_agent）。
        模型解析策略:
        - config.model != "inherit" 或 parent_model 已知或 app_config 已提供 → 立即解析
        - 其他情况 → 延迟到 _create_agent() 中解析（避免单元测试依赖 config.yaml）

        Args:
            config: 子代理配置对象。
            tools: 父代理的全部可用工具列表（将被过滤）。
            app_config: 应用配置对象，None 时延迟加载。
            parent_model: 父代理使用的模型名称，用于模型继承。
            sandbox_state: 父代理的沙箱状态，传递给子代理的 ThreadState。
            thread_data: 父代理的线程数据，传递给子代理的 ThreadState。
            thread_id: 线程 ID，用于沙箱操作和检查点。
            trace_id: 分布式追踪 ID，从父代理继承。
        """
        self.config = config
        self.app_config = app_config
        self.parent_model = parent_model
        # 延迟解析模型名称：仅在不依赖 config.yaml 时立即解析
        # 否则推迟到 _create_agent() 中（该方法已加载 app_config）
        if config.model != "inherit" or parent_model is not None or app_config is not None:
            self.model_name: str | None = resolve_subagent_model_name(config, parent_model, app_config=app_config)
        else:
            self.model_name = None
        self.sandbox_state = sandbox_state
        self.thread_data = thread_data
        self.thread_id = thread_id
        # 生成 trace_id（如果未提供，用于顶层调用的追踪）
        self.trace_id = trace_id or str(uuid.uuid4())[:8]

        # 根据配置过滤工具列表
        self._base_tools = _filter_tools(
            tools,
            config.tools,
            config.disallowed_tools,
        )
        self.tools = self._base_tools

        logger.info(f"[trace={self.trace_id}] SubagentExecutor initialized: {config.name} with {len(self.tools)} tools")

    def _create_agent(self, tools: list[BaseTool] | None = None):
        """创建 LangChain Agent 实例。

        使用子代理的模型配置和工具列表创建 Agent。中间件链复用
        build_subagent_runtime_middlewares() 的共享组合逻辑。
        system_prompt 不在此处注入，而是在 _build_initial_state() 中
        作为 SystemMessage 添加到消息列表，以避免某些 LLM API
        不支持多个 SystemMessage 的问题。

        Args:
            tools: 可选的工具列表覆盖。None 时使用 self.tools。

        Returns:
            LangChain Agent 可运行对象。
        """
        app_config = self.app_config or get_app_config()
        if self.model_name is None:
            self.model_name = resolve_subagent_model_name(self.config, self.parent_model, app_config=app_config)
        model = create_chat_model(name=self.model_name, thinking_enabled=False, app_config=app_config)

        from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares

        # 复用与主代理相同的中间件组合
        middlewares = build_subagent_runtime_middlewares(app_config=app_config, model_name=self.model_name, lazy_init=True)

        # system_prompt 在 _build_initial_state 中通过消息列表注入
        return create_agent(
            model=model,
            tools=tools if tools is not None else self.tools,
            middleware=middlewares,
            system_prompt=None,
            state_schema=ThreadState,
        )

    async def _load_skills(self) -> list[Skill]:
        """加载并过滤技能元数据。

        根据 config.skills 配置决定加载哪些技能:
        - None: 加载全部已启用的技能
        - []: 不加载任何技能
        - ["skill-a", "skill-b"]: 仅加载指定技能

        使用 asyncio.to_thread() 包装磁盘 I/O 操作，避免阻塞事件循环
        （LangGraph ASGI 服务器要求事件循环不被阻塞）。

        Returns:
            过滤后的技能列表。

        Raises:
            Exception: 当技能存储加载失败时重新抛出。
        """
        if self.config.skills is not None and len(self.config.skills) == 0:
            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} skills=[] — skipping skill loading")
            return []

        try:
            from deerflow.skills.storage import get_or_new_skill_storage

            storage_kwargs = {"app_config": self.app_config} if self.app_config is not None else {}
            storage = await asyncio.to_thread(get_or_new_skill_storage, **storage_kwargs)
            # 使用 asyncio.to_thread 避免阻塞事件循环（LangGraph ASGI 要求）
            all_skills = await asyncio.to_thread(storage.load_skills, enabled_only=True)
            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} loaded {len(all_skills)} enabled skills from disk")
        except Exception:
            logger.exception(f"[trace={self.trace_id}] Failed to load skills for subagent {self.config.name}")
            raise

        if not all_skills:
            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} no enabled skills found")
            return []

        # 根据 config.skills 白名单过滤
        if self.config.skills is not None:
            allowed = set(self.config.skills)
            return [s for s in all_skills if s.name in allowed]
        return all_skills

    def _apply_skill_allowed_tools(self, skills: list[Skill]) -> list[BaseTool]:
        """根据技能元数据的 allowed-tools 配置进一步限制工具列表。

        某些技能可能声明了 allowed-tools 字段，仅允许使用特定的工具。
        此方法将技能的工具约束应用到已过滤的工具列表上。

        Args:
            skills: 已加载的技能列表。

        Returns:
            经过技能工具约束过滤后的工具列表。
        """
        return filter_tools_by_skill_allowed_tools(self._base_tools, skills)

    async def _load_skill_messages(self, skills: list[Skill]) -> list[SystemMessage]:
        """将技能内容加载为 SystemMessage 消息列表。

        遵循 Codex 模式: 每个子代理在每次会话中独立加载自己的技能，
        并将其作为对话项（developer messages）注入，而非系统提示词文本。
        config.skills 白名单控制加载哪些技能。

        Args:
            skills: 已加载的技能列表。

        Returns:
            包含技能内容的 SystemMessage 列表。每个技能包裹在
            <skill name="..."> XML 标签中。
        """
        if not skills:
            return []

        # 读取每个技能的 SKILL.md 内容并创建对话消息项
        messages = []
        for skill in skills:
            try:
                content = await asyncio.to_thread(skill.skill_file.read_text, encoding="utf-8")
                content = content.strip()
                if content:
                    messages.append(SystemMessage(content=f'<skill name="{skill.name}">\n{content}\n</skill>'))
                    logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} loaded skill: {skill.name}")
            except Exception:
                logger.debug(f"[trace={self.trace_id}] Failed to read skill {skill.name}", exc_info=True)

        return messages

    async def _build_initial_state(self, task: str) -> tuple[dict[str, Any], list[BaseTool]]:
        """构建代理执行的初始状态。

        将系统提示词和技能内容合并为单个 SystemMessage（某些 LLM API 拒绝
        多个 SystemMessage，报错 "System message must be at the beginning"），
        然后添加用户任务作为 HumanMessage。

        Args:
            task: 任务描述文本。

        Returns:
            元组 (initial_state, filtered_tools):
            - initial_state: 包含 messages、sandbox、thread_data 的状态字典
            - filtered_tools: 经过技能工具约束过滤后的工具列表
        """
        # 加载技能作为对话消息项（Codex 模式）
        skills = await self._load_skills()
        filtered_tools = self._apply_skill_allowed_tools(skills)
        skill_messages = await self._load_skill_messages(skills)

        # 将 system_prompt 和技能内容合并为单个 SystemMessage
        # 某些 LLM API 拒绝多个 SystemMessage："System message must be at the beginning."
        system_parts: list[str] = []
        if self.config.system_prompt:
            system_parts.append(self.config.system_prompt)
        for skill_msg in skill_messages:
            system_parts.append(skill_msg.content)

        messages: list[Any] = []
        if system_parts:
            messages.append(SystemMessage(content="\n\n".join(system_parts)))

        # 添加实际任务消息
        messages.append(HumanMessage(content=task))

        state: dict[str, Any] = {
            "messages": messages,
        }

        # 透传父代理的沙箱和线程数据
        if self.sandbox_state is not None:
            state["sandbox"] = self.sandbox_state
        if self.thread_data is not None:
            state["thread_data"] = self.thread_data

        return state, filtered_tools

    async def _aexecute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """异步执行子代理任务。

        这是子代理执行的核心方法。流程:
        1. 构建初始状态（系统提示词 + 技能 + 任务）
        2. 创建 Agent 实例
        3. 通过 agent.astream() 流式执行
        4. 逐步收集 AI 消息，支持协作式取消检查
        5. 从最终状态提取结果文本
        6. 记录 token 用量并设置终态

        消息去重: 通过消息 ID 比较（如果有）或完整字典比较避免重复收集。
        取消检查: 在 astream 每次迭代边界检查 cancel_event。
        注意: 取消仅在 astream 迭代边界被检测，单次迭代中的长时间工具调用
        不会被中断直到下一个 chunk 产出。

        Args:
            task: 任务描述文本。
            result_holder: 可选的预创建结果对象（用于异步执行中的实时更新）。

        Returns:
            包含执行结果、AI 消息和 token 用量的 SubagentResult。
        """
        if result_holder is not None:
            # 使用提供的结果持有者（用于带实时更新的异步执行）
            result = result_holder
        else:
            # 为同步执行创建新结果对象
            task_id = str(uuid.uuid4())[:8]
            result = SubagentResult(
                task_id=task_id,
                trace_id=self.trace_id,
                status=SubagentStatus.RUNNING,
                started_at=datetime.now(),
            )
        ai_messages = result.ai_messages
        if ai_messages is None:
            ai_messages = []
            result.ai_messages = ai_messages

        collector: SubagentTokenCollector | None = None
        try:
            state, filtered_tools = await self._build_initial_state(task)
            agent = self._create_agent(filtered_tools)

            # Token 收集器用于跟踪子代理的 LLM 调用用量
            collector_caller = f"subagent:{self.config.name}"
            collector = SubagentTokenCollector(caller=collector_caller)

            # 构建运行配置：递归限制、回调、标签
            run_config: RunnableConfig = {
                "recursion_limit": self.config.max_turns,
                "callbacks": [collector],
                "tags": [collector_caller],
            }
            context: dict[str, Any] = {}
            if self.thread_id:
                run_config["configurable"] = {"thread_id": self.thread_id}
                context["thread_id"] = self.thread_id
            if self.app_config is not None:
                context["app_config"] = self.app_config

            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} starting async execution with max_turns={self.config.max_turns}")

            # 使用 astream 而非 invoke 以获取实时更新
            # 允许在生成过程中逐步收集 AI 消息
            final_state = None

            # 前置检查：在流式执行开始前检查是否已被取消
            if result.cancel_event.is_set():
                logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} cancelled before streaming")
                result.try_set_terminal(
                    SubagentStatus.CANCELLED,
                    error="Cancelled by user",
                    token_usage_records=collector.snapshot_records(),
                )
                return result

            async for chunk in agent.astream(state, config=run_config, context=context, stream_mode="values"):  # type: ignore[arg-type]
                # 协作式取消：检查父代理是否请求停止
                # 注意：取消仅在 astream 迭代边界被检测
                if result.cancel_event.is_set():
                    logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} cancelled by parent")
                    result.try_set_terminal(
                        SubagentStatus.CANCELLED,
                        error="Cancelled by user",
                        token_usage_records=collector.snapshot_records(),
                    )
                    return result

                final_state = chunk

                # 从当前状态提取 AI 消息
                messages = chunk.get("messages", [])
                if messages:
                    last_message = messages[-1]
                    # 检查是否为新的 AI 消息
                    if isinstance(last_message, AIMessage):
                        # 将消息转换为字典以便序列化
                        message_dict = last_message.model_dump()
                        # 仅添加未重复的消息（通过消息 ID 或完整字典比较去重）
                        message_id = message_dict.get("id")
                        is_duplicate = False
                        if message_id:
                            is_duplicate = any(msg.get("id") == message_id for msg in ai_messages)
                        else:
                            is_duplicate = message_dict in ai_messages

                        if not is_duplicate:
                            ai_messages.append(message_dict)
                            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} captured AI message #{len(ai_messages)}")

            logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} completed async execution")
            token_usage_records = collector.snapshot_records()
            final_result: str | None = None

            if final_state is None:
                logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no final state")
                final_result = "No response generated"
            else:
                # 提取最终消息 - 查找最后一条 AIMessage
                messages = final_state.get("messages", [])
                logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} final messages count: {len(messages)}")

                # 在对话中查找最后一条 AIMessage
                last_ai_message = None
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage):
                        last_ai_message = msg
                        break

                if last_ai_message is not None:
                    content = last_ai_message.content
                    # 处理 str 和 list 两种内容类型
                    if isinstance(content, str):
                        final_result = content
                    elif isinstance(content, list):
                        # 从内容块列表中提取文本，仅在最终结果中使用
                        # 连续的字符串片段直接拼接，完整文本块之间保留分隔以保持可读性
                        text_parts = []
                        pending_str_parts = []
                        for block in content:
                            if isinstance(block, str):
                                pending_str_parts.append(block)
                            elif isinstance(block, dict):
                                if pending_str_parts:
                                    text_parts.append("".join(pending_str_parts))
                                    pending_str_parts.clear()
                                text_val = block.get("text")
                                if isinstance(text_val, str):
                                    text_parts.append(text_val)
                        if pending_str_parts:
                            text_parts.append("".join(pending_str_parts))
                        final_result = "\n".join(text_parts) if text_parts else "No text content in response"
                    else:
                        final_result = str(content)
                elif messages:
                    # 回退：使用最后一条消息（即使不是 AIMessage）
                    last_message = messages[-1]
                    logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no AIMessage found, using last message: {type(last_message)}")
                    raw_content = last_message.content if hasattr(last_message, "content") else str(last_message)
                    if isinstance(raw_content, str):
                        final_result = raw_content
                    elif isinstance(raw_content, list):
                        parts = []
                        pending_str_parts = []
                        for block in raw_content:
                            if isinstance(block, str):
                                pending_str_parts.append(block)
                            elif isinstance(block, dict):
                                if pending_str_parts:
                                    parts.append("".join(pending_str_parts))
                                    pending_str_parts.clear()
                                text_val = block.get("text")
                                if isinstance(text_val, str):
                                    parts.append(text_val)
                        if pending_str_parts:
                            parts.append("".join(pending_str_parts))
                        final_result = "\n".join(parts) if parts else "No text content in response"
                    else:
                        final_result = str(raw_content)
                else:
                    logger.warning(f"[trace={self.trace_id}] Subagent {self.config.name} no messages in final state")
                    final_result = "No response generated"

            if final_result is None:
                final_result = "No response generated"

            result.try_set_terminal(
                SubagentStatus.COMPLETED,
                result=final_result,
                token_usage_records=token_usage_records,
            )

        except Exception as e:
            logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} async execution failed")
            result.try_set_terminal(
                SubagentStatus.FAILED,
                error=str(e),
                token_usage_records=collector.snapshot_records() if collector is not None else None,
            )

        return result

    def _execute_in_isolated_loop(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """在持久化事件循环中执行子代理。

        当同步 execute() 方法的调用方已在运行的事件循环中时使用此路径。
        因为 execute() 是同步 API，此方法阻塞调用方，同时实际协程在
        长生命周期的隔离循环上运行。复用该循环避免了共享的异步客户端
        （如 httpx 连接池）被绑定到短生命周期循环上。

        超时处理: 通过 Future.result(timeout=) 实现，超时后设置 cancel_event
        通知协作式取消，并取消 Future。

        Args:
            task: 任务描述文本。
            result_holder: 可选的预创建结果对象。

        Returns:
            子代理执行结果。

        Raises:
            FuturesTimeoutError: 执行超时。
        """
        future: Future[SubagentResult] | None = None
        parent_context = copy_context()
        try:
            future = _submit_to_isolated_loop_in_context(
                parent_context,
                lambda: self._aexecute(task, result_holder),
            )
            return future.result(timeout=self.config.timeout_seconds)
        except FuturesTimeoutError:
            # 超时时设置取消信号并取消 Future
            if result_holder is not None:
                result_holder.cancel_event.set()
            if future is not None:
                future.cancel()
            raise
        except Exception:
            if future is None:
                logger.debug(
                    f"[trace={self.trace_id}] Failed to submit subagent {self.config.name} to the isolated event loop",
                    exc_info=True,
                )
            else:
                logger.debug(
                    f"[trace={self.trace_id}] Subagent {self.config.name} failed while executing on the isolated event loop",
                    exc_info=True,
                )
            raise

    def execute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """同步执行子代理任务。

        自动检测调用环境并选择执行路径:
        - 如果调用方已在运行的事件循环中 → _execute_in_isolated_loop()
          （持久化循环路径，避免事件循环冲突）
        - 如果没有运行的事件循环 → asyncio.run()（标准路径）

        异常处理: 捕获所有异常并封装为 FAILED 状态的 SubagentResult，
        确保调用方始终获得有效的结果对象。

        Args:
            task: 任务描述文本。
            result_holder: 可选的预创建结果对象。

        Returns:
            包含执行结果的 SubagentResult。
        """
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                logger.debug(f"[trace={self.trace_id}] Subagent {self.config.name} detected running event loop, using isolated loop")
                return self._execute_in_isolated_loop(task, result_holder)

            # 标准路径：无运行中的事件循环，使用 asyncio.run
            return asyncio.run(self._aexecute(task, result_holder))
        except Exception as e:
            logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} execution failed")
            # 如果没有结果对象则创建一个包含错误信息的结果
            if result_holder is not None:
                result = result_holder
            else:
                result = SubagentResult(
                    task_id=str(uuid.uuid4())[:8],
                    trace_id=self.trace_id,
                    status=SubagentStatus.RUNNING,
                )
            result.try_set_terminal(SubagentStatus.FAILED, error=str(e))
            return result

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        """异步（后台）执行子代理任务。

        将任务提交到 _scheduler_pool 后立即返回 task_id。
        实际执行通过持久化事件循环进行，task_tool 以 5 秒间隔
        轮询 get_background_task_result() 获取执行进度。

        后台执行流程:
        1. 创建 PENDING 状态的 SubagentResult 并存入 _background_tasks
        2. 将 run_task 函数提交到 _scheduler_pool
        3. run_task 将状态更新为 RUNNING
        4. 通过 _submit_to_isolated_loop_in_context() 提交到持久化循环
        5. 等待 Future.result(timeout=) 或超时
        6. 超时时设置 TIMED_OUT 状态并取消 Future

        Args:
            task: 任务描述文本。
            task_id: 可选的任务 ID。None 时自动生成 UUID 前 8 位。

        Returns:
            可用于后续状态查询和结果获取的 task_id。
        """
        # 使用提供的 task_id 或生成新的
        if task_id is None:
            task_id = str(uuid.uuid4())[:8]

        # 创建初始 PENDING 状态结果
        result = SubagentResult(
            task_id=task_id,
            trace_id=self.trace_id,
            status=SubagentStatus.PENDING,
        )

        logger.info(f"[trace={self.trace_id}] Subagent {self.config.name} starting async execution, task_id={task_id}, timeout={self.config.timeout_seconds}s")

        with _background_tasks_lock:
            _background_tasks[task_id] = result

        parent_context = copy_context()

        # 提交到调度线程池
        def run_task():
            """后台任务执行函数，在 _scheduler_pool 线程中运行。"""
            with _background_tasks_lock:
                _background_tasks[task_id].status = SubagentStatus.RUNNING
                _background_tasks[task_id].started_at = datetime.now()
                result_holder = _background_tasks[task_id]

            try:
                # 直接提交到持久化事件循环，避免通过 execute() 创建临时循环
                execution_future = _submit_to_isolated_loop_in_context(
                    parent_context,
                    lambda: self._aexecute(task, result_holder),
                )
                try:
                    # 等待执行完成，带超时
                    execution_future.result(timeout=self.config.timeout_seconds)
                except FuturesTimeoutError:
                    logger.error(f"[trace={self.trace_id}] Subagent {self.config.name} execution timed out after {self.config.timeout_seconds}s")
                    # 发送协作式取消信号并取消 Future
                    result_holder.cancel_event.set()
                    result_holder.try_set_terminal(
                        SubagentStatus.TIMED_OUT,
                        error=f"Execution timed out after {self.config.timeout_seconds} seconds",
                    )
                    execution_future.cancel()
            except Exception as e:
                logger.exception(f"[trace={self.trace_id}] Subagent {self.config.name} async execution failed")
                with _background_tasks_lock:
                    task_result = _background_tasks[task_id]
                task_result.try_set_terminal(SubagentStatus.FAILED, error=str(e))

        _scheduler_pool.submit(run_task)
        return task_id


# ---------------------------------------------------------------------------
# 并发限制常量
# MAX_CONCURRENT_SUBAGENTS = 3 由 SubagentLimitMiddleware 在 after_model 阶段
# 截断多余的 task 工具调用。当主代理的 LLM 响应中包含超过此数量的 task 调用时，
# 中间件只保留前 3 个并发出警告日志。
# ---------------------------------------------------------------------------
MAX_CONCURRENT_SUBAGENTS = 3


def request_cancel_background_task(task_id: str) -> None:
    """请求取消正在运行的后台任务。

    设置任务的 cancel_event，_aexecute() 在 agent.astream() 迭代中
    协作式检查此信号。注意：子代理线程无法通过 Future.cancel() 强制终止，
    只能在下次迭代边界停止。

    Args:
        task_id: 要取消的任务 ID。
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is not None:
            result.cancel_event.set()
            logger.info("Requested cancellation for background task %s", task_id)


def get_background_task_result(task_id: str) -> SubagentResult | None:
    """获取后台任务的执行结果。

    由 task_tool 以 5 秒间隔轮询调用，直到任务进入终态。

    Args:
        task_id: execute_async() 返回的任务 ID。

    Returns:
        SubagentResult 如果找到，否则 None。
    """
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


def list_background_tasks() -> list[SubagentResult]:
    """列出所有后台任务。

    Returns:
        所有 SubagentResult 实例的列表。
    """
    with _background_tasks_lock:
        return list(_background_tasks.values())


def cleanup_background_task(task_id: str) -> None:
    """从后台任务存储中移除已完成的任务。

    应由 task_tool 在轮询完成并返回结果后调用，防止已完成任务
    在内存中持续累积导致内存泄漏。

    仅移除处于终态（COMPLETED/FAILED/TIMED_OUT/CANCELLED）的任务，
    避免与仍在更新任务条目的后台执行器产生竞态条件。

    Args:
        task_id: 要移除的任务 ID。
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is None:
            # 无需清理；可能已被移除
            logger.debug("Requested cleanup for unknown background task %s", task_id)
            return

        # 仅清理终态任务，避免与后台执行器的竞态条件
        if result.status.is_terminal or result.completed_at is not None:
            del _background_tasks[task_id]
            logger.debug("Cleaned up background task: %s", task_id)
        else:
            logger.debug(
                "Skipping cleanup for non-terminal background task %s (status=%s)",
                task_id,
                result.status.value if hasattr(result.status, "value") else result.status,
            )
