"""自定义技能管理工具（Skill Management Tool）

本模块实现了 `skill_manage` 工具，允许代理创建、编辑、修补、删除自定义技能，
以及管理技能的支持文件（如脚本文件）。

技能管理操作：
------------
- **create**：创建新的自定义技能（含 SKILL.md 内容）
- **edit**：替换现有自定义技能的 SKILL.md 内容
- **patch**：在现有 SKILL.md 中查找并替换文本片段
- **delete**：删除整个自定义技能
- **write_file**：向技能写入支持文件（如脚本）
- **remove_file**：删除技能的支持文件

安全机制：
--------
1. **命名验证**：通过 `SkillStorage.validate_skill_name()` 验证技能名称
2. **内容验证**：通过 `validate_skill_markdown_content()` 验证 SKILL.md 格式
3. **安全扫描**：通过 `scan_skill_content()` 对写入内容进行安全检查
   - 非可执行内容：block 级别的恶意内容会被阻止
   - 可执行内容（scripts/ 目录下的文件）：必须是 allow 级别才能写入
4. **并发控制**：每个技能名称有独立的 asyncio.Lock，防止并发修改冲突
5. **原子写入**：所有文件操作通过 SkillStorage 的方法执行

历史记录：
--------
每次修改操作都会记录到技能的历史文件中，包含：
- 操作类型（action）
- 作者（author = "agent"）
- 线程 ID（thread_id）
- 文件路径（file_path）
- 修改前/后的内容（prev_content / new_content）
- 安全扫描结果（scanner）

同步包装：
--------
skill_manage_tool 同时注册了异步 coroutine 和同步 func（通过
make_sync_tool_wrapper），使其在同步和异步调用路径中均可使用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from weakref import WeakValueDictionary

from langchain.tools import tool

from deerflow.agents.lead_agent.prompt import refresh_skills_system_prompt_cache_async
from deerflow.skills.security_scanner import scan_skill_content
from deerflow.skills.storage import get_or_new_skill_storage
from deerflow.skills.storage.skill_storage import SkillStorage
from deerflow.skills.types import SKILL_MD_FILE
from deerflow.tools.sync import make_sync_tool_wrapper
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

# 技能级别的异步锁字典
# 使用 WeakValueDictionary 避免内存泄漏：当没有引用持有锁时自动清理
_skill_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _get_lock(name: str) -> asyncio.Lock:
    """获取指定技能名称的异步锁。

    使用 WeakValueDictionary 管理锁的生命周期，避免长时间运行
    导致的内存泄漏。同一技能名称始终返回同一个 Lock 实例。
    """
    lock = _skill_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _skill_locks[name] = lock
    return lock


def _get_thread_id(runtime: Runtime | None) -> str | None:
    """从运行时上下文中解析当前线程 ID。

    优先从 runtime.context 获取，其次从 runtime.config 的
    configurable.thread_id 获取。
    """
    if runtime is None:
        return None
    if runtime.context and runtime.context.get("thread_id"):
        return runtime.context.get("thread_id")
    return runtime.config.get("configurable", {}).get("thread_id")


def _history_record(*, action: str, file_path: str, prev_content: str | None, new_content: str | None, thread_id: str | None, scanner: dict[str, Any]) -> dict[str, Any]:
    """构建技能操作的历史记录条目。

    Args:
        action: 操作类型（create/edit/patch/delete/write_file/remove_file）
        file_path: 操作的文件路径
        prev_content: 修改前的内容
        new_content: 修改后的内容
        thread_id: 当前线程 ID
        scanner: 安全扫描结果

    Returns:
        标准化的历史记录字典
    """
    return {
        "action": action,
        "author": "agent",
        "thread_id": thread_id,
        "file_path": file_path,
        "prev_content": prev_content,
        "new_content": new_content,
        "scanner": scanner,
    }


async def _scan_or_raise(content: str, *, executable: bool, location: str) -> dict[str, str]:
    """对写入内容执行安全扫描，如果被阻止则抛出异常。

    扫描策略：
    - 所有内容：如果 decision == "block"，直接抛出 ValueError
    - 可执行内容（scripts/ 目录下）：decision 必须为 "allow"，否则抛出异常
    - 非可执行内容：block 级别被阻止，warn 和 allow 都允许

    Args:
        content: 待扫描的内容
        executable: 是否为可执行内容（scripts/ 目录下的文件）
        location: 文件位置描述（用于安全扫描报告）

    Returns:
        包含 decision 和 reason 的字典

    Raises:
        ValueError: 如果安全扫描阻止了写入
    """
    result = await scan_skill_content(content, executable=executable, location=location)
    if result.decision == "block":
        raise ValueError(f"Security scan blocked the write: {result.reason}")
    if executable and result.decision != "allow":
        raise ValueError(f"Security scan rejected executable content: {result.reason}")
    return {"decision": result.decision, "reason": result.reason}


async def _to_thread(func, /, *args, **kwargs):
    """在线程池中执行同步函数（asyncio.to_thread 的简单封装）。"""
    return await asyncio.to_thread(func, *args, **kwargs)


async def _skill_manage_impl(
    runtime: Runtime,
    action: str,
    name: str,
    content: str | None = None,
    path: str | None = None,
    find: str | None = None,
    replace: str | None = None,
    expected_count: int | None = None,
) -> str:
    """自定义技能管理的核心实现。

    支持六种操作：

    1. **create** — 创建新技能
       - 验证技能名称不重复
       - 验证 SKILL.md 内容格式
       - 安全扫描内容
       - 写入 SKILL.md 文件
       - 刷新系统提示缓存

    2. **edit** — 替换技能的 SKILL.md
       - 确保技能可编辑
       - 记录修改前的内容
       - 安全扫描新内容
       - 写入并记录历史

    3. **patch** — 查找并替换 SKILL.md 中的文本
       - 精确查找目标文本
       - 支持预期替换次数验证
       - 安全扫描替换后的内容

    4. **delete** — 删除整个技能
       - 删除技能目录及其所有内容

    5. **write_file** — 写入支持文件
       - 验证文件路径安全性（ensure_safe_support_path）
       - scripts/ 目录下的文件视为可执行内容，安全扫描更严格

    6. **remove_file** — 删除支持文件
       - 验证文件存在后删除

    所有操作都受技能级别的异步锁保护，防止并发修改冲突。

    Args:
        runtime: 工具运行时（包含线程状态和配置）
        action: 操作类型（create/patch/edit/delete/write_file/remove_file）
        name: 技能名称（hyphen-case 格式）
        content: 文件内容（用于 create、edit 或 write_file）
        path: 支持文件路径（用于 write_file 或 remove_file）
        find: 要替换的文本（用于 patch）
        replace: 替换后的文本（用于 patch）
        expected_count: 预期的替换次数（用于 patch，可选）

    Returns:
        操作结果描述字符串

    Raises:
        ValueError: 参数无效、技能不存在/已存在、安全扫描被阻止等
    """
    name = SkillStorage.validate_skill_name(name)
    lock = _get_lock(name)  # 获取技能级别的锁
    thread_id = _get_thread_id(runtime)
    skill_storage = get_or_new_skill_storage()

    async with lock:  # 技能级别的互斥锁，防止并发修改
        if action == "create":
            # ── 创建新技能 ──
            if await _to_thread(skill_storage.custom_skill_exists, name):
                raise ValueError(f"Custom skill '{name}' already exists.")
            if content is None:
                raise ValueError("content is required for create.")
            await _to_thread(skill_storage.validate_skill_markdown_content, name, content)
            scan = await _scan_or_raise(content, executable=False, location=f"{name}/{SKILL_MD_FILE}")
            await _to_thread(skill_storage.write_custom_skill, name, SKILL_MD_FILE, content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="create", file_path=SKILL_MD_FILE, prev_content=None, new_content=content, thread_id=thread_id, scanner=scan),
            )
            # 刷新技能系统提示缓存，使新技能立即生效
            await refresh_skills_system_prompt_cache_async()
            return f"Created custom skill '{name}'."

        if action == "edit":
            # ── 替换整个 SKILL.md 内容 ──
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if content is None:
                raise ValueError("content is required for edit.")
            await _to_thread(skill_storage.validate_skill_markdown_content, name, content)
            scan = await _scan_or_raise(content, executable=False, location=f"{name}/{SKILL_MD_FILE}")
            skill_file = skill_storage.get_custom_skill_file(name)
            prev_content = await _to_thread(skill_file.read_text, encoding="utf-8")
            await _to_thread(skill_storage.write_custom_skill, name, SKILL_MD_FILE, content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="edit", file_path=SKILL_MD_FILE, prev_content=prev_content, new_content=content, thread_id=thread_id, scanner=scan),
            )
            await refresh_skills_system_prompt_cache_async()
            return f"Updated custom skill '{name}'."

        if action == "patch":
            # ── 查找并替换 SKILL.md 中的文本 ──
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if find is None or replace is None:
                raise ValueError("find and replace are required for patch.")
            skill_file = skill_storage.get_custom_skill_file(name)
            prev_content = await _to_thread(skill_file.read_text, encoding="utf-8")
            occurrences = prev_content.count(find)
            if occurrences == 0:
                raise ValueError("Patch target not found in SKILL.md.")
            if expected_count is not None and occurrences != expected_count:
                raise ValueError(f"Expected {expected_count} replacements but found {occurrences}.")
            replacement_count = expected_count if expected_count is not None else 1
            new_content = prev_content.replace(find, replace, replacement_count)
            await _to_thread(skill_storage.validate_skill_markdown_content, name, new_content)
            scan = await _scan_or_raise(new_content, executable=False, location=f"{name}/{SKILL_MD_FILE}")
            await _to_thread(skill_storage.write_custom_skill, name, SKILL_MD_FILE, new_content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="patch", file_path=SKILL_MD_FILE, prev_content=prev_content, new_content=new_content, thread_id=thread_id, scanner=scan),
            )
            await refresh_skills_system_prompt_cache_async()
            return f"Patched custom skill '{name}' ({replacement_count} replacement(s) applied, {occurrences} match(es) found)."

        if action == "delete":
            # ── 删除整个技能 ──
            await _to_thread(
                skill_storage.delete_custom_skill,
                name,
                history_meta=_history_record(
                    action="delete",
                    file_path=SKILL_MD_FILE,
                    prev_content=None,
                    new_content=None,
                    thread_id=thread_id,
                    scanner={"decision": "allow", "reason": "Deletion requested."},
                ),
            )
            await refresh_skills_system_prompt_cache_async()
            return f"Deleted custom skill '{name}'."

        if action == "write_file":
            # ── 写入支持文件 ──
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if path is None or content is None:
                raise ValueError("path and content are required for write_file.")
            # 验证文件路径安全性（防止路径遍历攻击）
            target = await _to_thread(skill_storage.ensure_safe_support_path, name, path)
            exists = await _to_thread(target.exists)
            prev_content = await _to_thread(target.read_text, encoding="utf-8") if exists else None
            # scripts/ 目录下的文件视为可执行内容，安全扫描更严格
            executable = "scripts/" in path or path.startswith("scripts/")
            scan = await _scan_or_raise(content, executable=executable, location=f"{name}/{path}")
            await _to_thread(skill_storage.write_custom_skill, name, path, content)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="write_file", file_path=path, prev_content=prev_content, new_content=content, thread_id=thread_id, scanner=scan),
            )
            return f"Wrote '{path}' for custom skill '{name}'."

        if action == "remove_file":
            # ── 删除支持文件 ──
            await _to_thread(skill_storage.ensure_custom_skill_is_editable, name)
            if path is None:
                raise ValueError("path is required for remove_file.")
            target = await _to_thread(skill_storage.ensure_safe_support_path, name, path)
            if not await _to_thread(target.exists):
                raise FileNotFoundError(f"Supporting file '{path}' not found for skill '{name}'.")
            prev_content = await _to_thread(target.read_text, encoding="utf-8")
            await _to_thread(target.unlink)
            await _to_thread(
                skill_storage.append_history,
                name,
                _history_record(action="remove_file", file_path=path, prev_content=prev_content, new_content=None, thread_id=thread_id, scanner={"decision": "allow", "reason": "Deletion requested."}),
            )
            return f"Removed '{path}' from custom skill '{name}'."

        # 检查是否为内置技能
        if await _to_thread(skill_storage.public_skill_exists, name):
            raise ValueError(f"'{name}' is a built-in skill. To customise it, create a new skill with the same name under skills/custom/.")
        raise ValueError(f"Unsupported action '{action}'.")


@tool("skill_manage", parse_docstring=True)
async def skill_manage_tool(
    runtime: Runtime,
    action: str,
    name: str,
    content: str | None = None,
    path: str | None = None,
    find: str | None = None,
    replace: str | None = None,
    expected_count: int | None = None,
) -> str:
    """Manage custom skills under skills/custom/.

    管理自定义技能（位于 skills/custom/ 目录下）。

    Args:
        action: 操作类型，支持 create、patch、edit、delete、write_file、remove_file。
        name: 技能名称，使用 hyphen-case 格式（如 my-custom-skill）。
        content: 文件内容，用于 create、edit 或 write_file 操作。
        path: 支持文件路径，用于 write_file 或 remove_file 操作。
        find: 要查找的文本，用于 patch 操作。
        replace: 替换后的文本，用于 patch 操作。
        expected_count: 预期的替换次数（可选），用于 patch 操作。
    """
    return await _skill_manage_impl(
        runtime=runtime,
        action=action,
        name=name,
        content=content,
        path=path,
        find=find,
        replace=replace,
        expected_count=expected_count,
    )


# 为同步调用路径附加同步包装器（嵌入式 DeerFlowClient 需要）
skill_manage_tool.func = make_sync_tool_wrapper(_skill_manage_impl, "skill_manage")
