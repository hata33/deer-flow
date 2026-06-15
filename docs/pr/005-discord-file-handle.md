# 005 — fix(channels): close Discord file handle after upload

| 字段 | 值 |
|------|-----|
| PR | #3561 |
| 关联 Issue | #3544（Refs，非 Closes —— umbrella 还含 dedupe/rate-limit 等其他 bullet） |
| Commit | `1783da42` |
| 状态 | Merged |
| 合并者 | WillemJiang |
| 合并时间 | 2026-06-13 |
| 分支 | fix/discord-file-handle-leak → upstream/main |
| 改动规模 | 2 文件 +134/-5 行 |
| 目标 bullet | #3544 中「Use context-managed file handles for Discord uploads」 |

## 问题

Issue #3544 是 collaborator ShenAC-SAC 开的 channel 加固 umbrella，其中一条：

> Use context-managed file handles for Discord uploads
> Validation: Discord upload closes file handles

Discord 通道每次向用户投递文件时，`send_file` 用裸 `open()` 打开附件后**全程不关闭**，导致每次出站文件投递都泄漏一个文件描述符。

## 根因

`backend/app/channels/discord.py` 的 `send_file`（出站文件投递）：

```
ChannelManager._prepare_artifact_delivery()        manager.py:577
  └─ _resolve_attachments()  virtual_path→actual_path
  └─ OutboundMessage(attachments=[ResolvedAttachment])
        ↓ MessageBus.publish_outbound
Channel._on_outbound(msg)                          base.py:123
  └─ for att in msg.attachments:
        await self.send_file(msg, att)             base.py:141
              ↓
DiscordChannel.send_file(msg, attachment)          discord.py:195
  ├─ fp = open(actual_path, "rb")  # noqa: SIM115  ← 开了不关
  ├─ file = discord.File(fp, ...)
  ├─ await wrap_future(run_coroutine_threadsafe(target.send(file), _discord_loop))
  └─ return True                                    ← fp 永不 close
```

两个泄漏点：
1. **成功路径**：`fp` 开了没有任何 `close()`，函数返回后 fd 悬空
2. **异常路径**：`target.send` 抛错时，`except` 分支只 log + return，同样不关 `fp`

`# noqa: SIM115` 是有人用 lint 抑制注释压掉了 ruff「文件打开要用 with」的规则，而不是修掉它。

横向对比发现 **Discord 是唯一漏网者**：telegram（`send_photo`/`send_document`）和 feishu（`_upload_image`/`_upload_file`）都已经在用 `with open(...)`。

## 方案

把 `open()` 包进 `with` 语句，覆盖整个上传过程：

```python
try:
    with open(str(attachment.actual_path), "rb") as fp:
        file = self._discord_module.File(fp, filename=attachment.filename)
        send_future = asyncio.run_coroutine_threadsafe(target.send(file=file), self._discord_loop)
        await asyncio.wrap_future(send_future)
    logger.info("[Discord] file uploaded: %s", attachment.filename)
    return True
except Exception:
    logger.exception("[Discord] failed to upload file: %s", attachment.filename)
    return False
```

### 时序正确性（关键）

Discord 是**双线程**模型，这是 `with` 能否放对位置的核心：

- `send_file`（async）跑在 channels 主事件循环
- `discord.py` 客户端跑在独立线程 `_discord_loop`
- 跨线程靠 `run_coroutine_threadsafe(coro, _discord_loop)` + `await wrap_future(fut)`

discord.py 在 `target.send(file)` 内部构造 multipart HTTP 请求时**同步读取 `fp`**（含内部重试时 seek(0) 重读）。当 `await wrap_future(send_future)` resolve 时，意味着 `target.send` 整个协程已返回 → **fp 已被完整消费** → 此时关闭安全。

`with` 必须包住 `File 构造 + send + await wrap_future` 三行；退出 `with` 时关闭。成功和异常两条路径都保证关闭。

## 取舍

| 选择 | 理由 |
|------|------|
| 用 `with` 而非手动 `fp.close()` | `with` 在异常路径也保证关闭，手动 close 需要写 try/finally，更易漏 |
| **不顺手 offload 到线程**（`asyncio.to_thread`） | #3544 要的只是 context manager 修泄漏；offload 是**另一个独立关注点**，硬塞进来是 scope creep，且 blast radius 变大 |
| 引用用 `Refs #3544` 而非 `Closes #3544` | #3544 是 umbrella，只修了其中一个 bullet，`Closes` 会误关整个 issue |
| 只动 `send_file` 一个函数 | `_stop_typing`/`_resolve_target` 调用原样未动；只碰 discord.py，不碰 telegram/feishu/manager/base |

**放弃的方案**：
- 读进 `BytesIO` 再传 —— 改变内存行为（整文件入内存），不必要
- offload `open()` 到线程 —— 超出 issue scope，留给独立的 blocking-IO 议题

## 贡献选点过程（教训）

这次选点踩了 DeerFlow 当前的贡献节奏：

- **所有带复现步骤的 bug issue 几小时内就被认领**：实测 #3364（2 个 PR）、#3395、#3380、#3000/#3001/#2999 全部已有 open PR；#3536 技术债也被 assign 给核心 maintainer hetaoBackend。
- 仓库有 triage 机器人（fancy-agent / fancyboi999）+ 快速认领者，扫到候选后**必须先 `gh pr list --search 编号` + 查 assignees** 再投入。
- 剩下的开放车道只有两类：
  - **(A)** collaborator 开的 umbrella 里、**未 assign** 的单个窄 bullet（本 PR 即此）
  - **(B)** 自助代码检测：`make detect-blocking-io`

教训已写入记忆 [[experience-pr-selection]]。

## 验证

- **测试真实性**（PR 模板要求）：临时把 `discord.py` 还原到 `upstream/main`（无修复版本），两个新测试**全部 RED**（`assert handles[0].closed is True` 失败）；恢复修复后**全部 GREEN**。证明测试真的能抓到 bug。
- 新增 2 个回归测试（`tests/test_discord_channel.py`）：
  - `test_send_file_closes_file_handle`（成功路径句柄关闭）
  - `test_send_file_closes_handle_when_send_fails`（异常路径句柄仍关闭）
  - 用真实后台 event loop 忠实复现 `run_coroutine_threadsafe` + `wrap_future` 跨线程机制
- `tests/test_discord_channel.py` 全部 7 个测试通过（5 既有 + 2 新增）
- channel 相关 3 个测试文件共 **207 passed**，无回归
- `ruff check .`（全量）+ `ruff format --check` 通过
- CI gate 分析：blocking-IO gate 只跑 `tests/blocking_io/`（我的测试不在其中）；`open()` 是 main 既有、非新增阻塞 IO；CI 跑 Linux，本地 Windows 上 2 个 symlink 测试的 `WinError 1314` 在 CI 不会出现

## 后续

- PR #3561 已由 WillemJiang 于 2026-06-13 合并（merge commit `1783da42`）
