# DeerFlow 贡献指南：如何提交一个干净的 PR

> 基于实际踩坑经验总结（StepFun adapter PR #3456 / #3461）

## 核心原则

**一个 PR 只做一件事，只包含相关的文件改动。**

## 正确流程

### 1. 从 upstream/main 创建全新分支

```bash
git fetch upstream
git checkout -B feat/your-feature upstream/main
```

⚠️ **不要从自己的本地分支（如 `main-local`）创建**，否则会带入无关历史（merge commit、initial commit 等），导致 PR diff 膨胀到几万行。

### 2. 只写/只改你需要的文件

写代码、写测试、改配置示例，只动跟你功能相关的文件。

### 3. 提交前验证（必须！）

```bash
# 检查改动文件数和行数，必须符合预期
git diff --stat upstream/main

# 逐文件检查 diff，确保没有回退别人的改动
git diff upstream/main -- path/to/file
```

**预期判断**：
- 一个 model adapter：~3 个文件，~500 行
- 如果显示几十个文件、几万行，说明分支不干净，立即停下排查

### 4. 写好 Commit Message

DeerFlow 使用 **Squash Merge**，所有 PR 最终压成一个 commit。你的 commit message 会直接进入主分支历史。

以下规则基于仓库最近 60 个已合并 PR 的 commit 统计得出。

#### 格式

```
type(scope): 简短描述
```

> `(#PR_NUMBER)` 不需要手动写，GitHub squash merge 时会自动追加。

#### Type（必选）

统计分布（最近 60 个 PR）：`fix` 40 个 · `feat` 7 个 · `refactor` 3 个 · `chore` 3 个 · `docs` 3 个 · `test` 2 个

| type | 用途 | 仓库真实示例 |
|------|------|-------------|
| `fix` | Bug 修复（占 67%） | `fix(mcp): accept transport field as alias for type` |
| `feat` | 新功能（占 12%） | `feat(agent): add ToolOutputBudgetMiddleware` |
| `refactor` | 重构 | `refactor(tool-search): consolidate MCP metadata tag` |
| `chore` | 杂项 | `chore: remove stale LangGraph server runtime remnants` |
| `docs` | 文档 | `docs: clean gateway runtime transition remnants` |
| `test` | 测试 | `test(runtime): add Blockbuster runtime anchor` |

#### Scope（推荐，跨模块时省略）

仓库中实际使用的 scope 按频率排序：

| 高频 scope | 说明 | 示例 |
|-----------|------|------|
| `frontend` | 前端 UI | `fix(frontend): truncate overflowing text in agent cards` |
| `runtime` | 运行时 | `fix(runtime): protect sync singleton init and reset` |
| `middleware` | 中间件 | `fix(middleware): fix LLM fallback run status` |
| `mcp` | MCP 集成 | `fix(mcp): add auth interceptor with channel user_id` |
| `agents` | Agent 系统 | `fix(agents): harden update_agent null-like args` |
| `channels` | IM 渠道 | `fix(channels): preserve Feishu clarification thread continuity` |
| `provider` | 模型供应商 | `feat(provider) Add patched MiMo reasoning content support` |

| 低频 scope | 说明 | 示例 |
|-----------|------|------|
| `sandbox` | 沙箱 | `fix(sandbox): close AioSandbox HTTP client during provider teardown` |
| `config` | 配置系统 | `fix(config): make the reload boundary discoverable from code` |
| `security` | 安全 | `fix(security): harden MCP config endpoint` |
| `search` | 搜索 | `fix(search): fix DDGS Wikipedia region handling` |
| `ux` | 用户体验 | `fix(ux): remove Backspace shortcut for deleting prompt attachments` |
| `e2e` | E2E 测试 | `test(e2e): deterministic record/replay front-back contract verification` |

**跨模块/杂项时省略 scope**（占 ~20%）：

```
chore: add AI assistance disclosure to PR template and CONTRIBUTING
feat: upgrade MiniMax default model to M3
docs: add blocking IO detection usage and maintenance
fix: load paginated run history messages
```

#### Body（推荐，用空行与 subject 隔开）

**小改动** — 用 `-` 列出要点：
```
feat: upgrade MiniMax default model to M3 (#3357)

- Add MiniMax-M3 to model list and set as default
- Keep MiniMax-M2.7 and MiniMax-M2.7-highspeed
- Remove older models (M2.5)
- Update related tests
```

**大改动** — 先概述背景和原因，再列变更：
```
feat(agent): add ToolOutputBudgetMiddleware for oversized tool output protection (#3303)

Closes #3289. Adds a unified middleware that enforces per-result budgets
on ALL tool outputs (MCP, sandbox, community, custom), preventing
oversized external tool results from blowing the model context window.

Key features:
- Disk externalization: oversized outputs written to thread-local directory
- Fallback truncation: head+tail truncation when disk is unavailable
- read_file exemption: prevents persist-read-persist infinite loops
- Per-tool threshold overrides via config
```

**多轮 review 后的 squash** — 用 `*` 列出每次迭代：
```
feat(provider) Add patched MiMo reasoning content support (#3298)

* Add patched MiMo reasoning content support
* Clarify MiMo patched model coverage
* Remove unused MiMo payload index
* Address MiMo review nits
```

#### 规则总结

| 规则 | 正确 | 错误 |
|------|------|------|
| 祈使句 | `add StepFun adapter` | `added StepFun adapter` |
| scope 用模块名 | `(models)` | `(patched_stepfun)` |
| 有 scope 加冒号 | `fix(mcp):` | `fix mcp:` |
| 全小写描述 | `add StepFun adapter` | `Add StepFun Adapter` |
| 不要无意义标题 | `fix(mcp): accept transport field as alias` | `update code` |

### 5. 推送并创建 PR

```bash
git push origin feat/your-feature --force-with-lease
```

然后在 GitHub 创建 PR，base 选择 `bytedance:main`，head 选择你的 fork 分支。

## 常见陷阱

### 陷阱 1：分支 base 不干净

**症状**：PR 显示大量无关文件改动。

**原因**：分支是从本地分支创建的，带入了 `Initial commit`、`Merge remote-tracking branch` 等历史。

**解决**：始终从 `upstream/main` 的 HEAD 创建新分支。

### 陷阱 2：用 `git checkout <old-commit> -- file` 拉整个文件

**症状**：PR 中出现回退其他贡献者改动的情况（如 MiniMax 配置被还原）。

**原因**：`git checkout <commit> -- file` 会拉取该 commit 时文件的完整版本，覆盖掉其他人在这之后的改动。

**解决**：
- 手动编辑目标文件，只加入你的改动
- 或者在编辑前先确认 upstream 最新版本的 diff

### 陷阱 3：docstring / 注释风格不统一

**症状**：模块顶部注释过长（50+ 行），而同类文件只有 6-13 行。

**解决**：提交前参考同目录下的现有文件风格，保持一致。配置示例和 API 文档链接放在 `config.example.yaml` 里，不要在模块 docstring 中重复。

### 陷阱 4：忘记 rebase 到最新 upstream

**症状**：PR 标记 "This branch is out-of-date with the base branch"。

**解决**：
```bash
git fetch upstream
git rebase upstream/main
git push --force-with-lease origin feat/your-feature
```

## PR 描述填写指南

PR 描述和 commit message 是两回事。Commit message 进 git 历史，PR 描述在 GitHub 上给 reviewer 看。
DeerFlow 有标准模板（`.github/PULL_REQUEST_TEMPLATE.md`），以下是每个字段的写法。

### 关联 Issue（可选）

```
Fixes #123       ← 自动关闭 issue
Closes #456
Resolves #789
```

没有关联 issue 就删掉这行。

### Why — 为什么要做这个改动

回答两个问题：**触发原因** + **解决的痛点**。不要写代码层面的内容。

**好的写法**（StepFun adapter PR）：
```
StepFun provides OpenAI-compatible reasoning models (step-3.7-flash, step-3.5-flash).
However, LangChain's ChatOpenAI drops the non-standard reasoning / reasoning_content
fields. This causes:

- Loss of reasoning visibility in multi-turn conversations
- API errors when historical assistant messages miss reasoning_content
```

**不好的写法**：
```
~我想加个 StepFun 支持~           ← 太模糊
~因为 StepFun 有 reasoning 字段~  ← 只说了技术细节，没说痛点
```

### What changed — 改了什么

从**用户/调用者**角度描述，不是代码 diff。

**好的写法**：
```
- Add PatchedChatStepFun adapter that captures reasoning from both streaming
  and non-streaming responses
- Replay reasoning onto historical assistant messages in _get_request_payload
- Support both reasoning and reasoning_content field names
- Add 17 unit tests
```

**不好的写法**：
```
~修改了 patched_stepfun.py，加了 _extract_reasoning 函数~
← 这是代码 diff，不是用户视角
```

### Surface area — 影响范围

只勾选**跟你 PR 相关的**。Reviewer 用这个来决定审查范围。

```
- [ ] **Frontend UI**
- [ ] **Backend API**
- [x] **Agents / LangGraph** — new model adapter
- [ ] **Sandbox**
- [ ] **Skills**
- [ ] **Dependencies**
- [ ] **Default behavior change**
- [x] **Docs / tests / CI only** — config example + tests
```

### Validation — 你跑了什么命令

写出**你实际跑过的命令和结果**，不要只写"测试通过"。

```
cd backend && PYTHONPATH=. uv run pytest tests/test_patched_stepfun.py -v
# 17 passed
```

按影响区域选择对应的验证命令：

| 改了什么 | 跑什么 |
|---------|--------|
| 后端代码 | `cd backend && make lint && make test` |
| 前端代码 | `cd frontend && pnpm format && pnpm lint && pnpm typecheck && pnpm build` |
| 前端 E2E | `cd frontend && make test-e2e` |

### AI assistance — AI 工具披露

**必须如实填写**，DeerFlow 是 AI 项目，reviewer 只是用来校准审查力度，不会因为你用了 AI 而拒绝。

```
**Tool(s) used:** Claude Code

**How you used it:** AI assisted with adapter implementation and test writing;
reviewed and refined by author.

- [x] I've read and understand every line of this change and take responsibility
     for it.
```

### 完整 PR 描述示例

**小改动**（如升级默认模型）：

```
Closes #3289

## Why
MiniMax M3 is now available with better performance and reasoning capabilities.

## What changed
- Default MiniMax model changed from M2.5 to M3
- Older models (M2.5) removed from config
- Related tests updated

## Surface area
- [x] **Docs / tests / CI only**

## Validation
cd backend && make test
# All passed

## AI assistance
**Tool(s) used:** none
**How you used it:** N/A
- [x] I've read and understand every line of this change.
```

**新功能**（如添加 model adapter）：

```
## Why
StepFun provides OpenAI-compatible reasoning models (step-3.7-flash, step-3.5-flash),
but LangChain's ChatOpenAI silently drops the non-standard reasoning / reasoning_content
fields. This breaks reasoning visibility and causes API errors in multi-turn conversations.

## What changed
- Add PatchedChatStepFun adapter that captures reasoning from both streaming and
  non-streaming responses
- Replay reasoning onto historical assistant messages via _get_request_payload
- Support both `reasoning` and `reasoning_content` field names
- Add 17 unit tests

## Surface area
- [x] **Agents / LangGraph** — new model adapter
- [x] **Docs / tests / CI only** — config example + tests

## Validation
cd backend && PYTHONPATH=. uv run pytest tests/test_patched_stepfun.py -v
# 17 passed

## AI assistance
**Tool(s) used:** Claude Code
**How you used it:** AI assisted with adapter implementation and test writing;
reviewed and refined by author.
- [x] I've read and understand every line of this change.
```

**重开 PR 时**（如之前被关闭），在描述最前面加一句：

```
This PR replaces #3456 (closed). Previous PR contained unrelated changes;
this one is scoped to the StepFun adapter only.
```

## Checklist（提交前过一遍）

- [ ] 分支是从 `upstream/main` 最新代码创建的
- [ ] `git diff --stat upstream/main` 显示的文件数符合预期
- [ ] 没有回退其他人的改动（逐文件 `git diff` 检查）
- [ ] 代码风格与同目录现有文件一致
- [ ] 测试通过
- [ ] Commit message 遵循 Conventional Commits
- [ ] PR body 填写了所有必填字段
