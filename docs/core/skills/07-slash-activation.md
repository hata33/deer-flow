# 07 - Slash Skill 激活机制：跨渠道的技能即时加载

> 本文档分析 PR #3466 引入的 Slash Skill 激活系统——用户输入 `/skill-name 任务描述` 即可按需加载完整技能，覆盖 Web、IM（Slack/Telegram/Discord/飞书/钉钉/企微）全渠道。

---

## 一、问题背景

### 之前的方式

技能内容在 agent 初始化时**全部预加载到 system prompt**。这意味着：

1. 每轮对话都携带所有技能的完整内容，浪费 token
2. 用户无法显式指定"这一轮我想用哪个技能"
3. IM 渠道（Slack/Telegram 等）的文本格式与 Web 不同，`/command` 语法在 IM 中常被平台拦截或转义

### 新方式

用户输入 `/skill-name 任务描述`，中间件在模型调用前动态注入该技能的完整 SKILL.md 内容。不激活时不加载，token 按需消耗。

---

## 二、架构全貌

```
用户输入 "/graphify 分析这个 URL"
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 渠道适配层 (Channel Manager)                             │
│                                                         │
│ Web → input-box.tsx → autocomplete → 原文透传            │
│ Slack → @bot /graphify ... → command.text 提取           │
│ Telegram → /graphify ... → message.text 直传            │
│ Discord → /graphify ... → interaction.data.resolved     │
│ 飞书/钉钉/企微 → 各自的命令解析 → 统一文本               │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│ SkillActivationMiddleware (wrap_model_call)              │
│                                                         │
│ 1. _find_activation_target(messages)                     │
│    ├── 倒序找最后一条用户消息                             │
│    ├── 跳过 summary / hide_from_ui 消息                  │
│    └── 检查是否已有 activation（幂等性）                  │
│                                                         │
│ 2. _resolve_activation(text)                             │
│    ├── parse_slash_skill_reference(text)                  │
│    │   └── 正则 ^/([a-z0-9-]+)(?:\s+|$)                 │
│    │   └── 排除保留名: bootstrap/help/memory/models/...  │
│    ├── 加载全部技能 → 找到匹配                           │
│    ├── 检查 enabled / available_skills 白名单             │
│    └── 读取 SKILL.md → SHA256 哈希                       │
│                                                         │
│ 3. _build_activation_reminder(activation)                │
│    └── 构造 XML 格式的技能注入消息                       │
│                                                         │
│ 4. 插入到目标消息之前                                    │
│    messages.insert(target_index, activation_msg)         │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
               LLM 收到完整技能内容 + 用户任务
```

---

## 三、核心组件详解

### 1. slash.py — 命令解析层

两个数据类 + 两个解析函数，纯值对象，无副作用：

```python
# 保留名：/help、/memory、/models 等是系统命令，不是技能
RESERVED_SLASH_SKILL_NAMES = frozenset({"bootstrap", "help", "memory", "models", "new", "status"})

# 严格语法：小写字母+数字+连字符，后跟空格或行尾
_SLASH_SKILL_RE = re.compile(r"^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)")
```

**解析流程**：

```
输入文本 → parse_slash_skill_reference()
  ├── 不匹配正则 → None（不是 slash skill 命令）
  ├── 匹配保留名 → None（是系统命令）
  └── 匹配技能名 → SlashSkillReference(name, remaining_text)

SlashSkillReference → resolve_slash_skill()
  ├── available_skills 白名单检查
  ├── 从 skills 列表找到匹配且 enabled 的技能
  └── ResolvedSlashSkill(skill, remaining_text, container_file_path)
```

### 2. SkillActivationMiddleware — 注入层

**生命周期**：在 `wrap_model_call` / `awrap_model_call` 中执行，即每次 LLM 调用前。

**三阶段处理**：

#### 阶段 1：找到激活目标

```python
def _find_activation_target(self, messages):
    # 倒序找最后一条可激活的用户消息
    target_index = next((idx for idx in range(len(messages)-1, -1, -1)
                         if _is_user_activation_target(messages[idx])), None)
    # 幂等检查：已经激活过的不再激活
    if self._has_existing_activation_for_target(messages, target_index, target):
        return None
```

跳过的消息类型：
- `message.name == "summary"` — 上下文压缩产生的摘要
- `additional_kwargs.hide_from_ui == True` — 隐藏的系统消息
- 已有 `slash_skill_activation` 标记的 — 幂等保护

#### 阶段 2：解析并验证

```python
def _resolve_activation(self, text):
    reference = parse_slash_skill_reference(text)
    if reference is None:
        return None  # 不是 slash 命令

    skills = storage.load_skills(enabled_only=False)
    skill = next((s for s in skills if s.name == reference.name), None)

    if skill is None:
        return _ActivationResolution(failure_message="Skill `/{name}` is not installed.")
    if not skill.enabled:
        return _ActivationResolution(failure_message="Skill `/{name}` is installed but disabled.")
    if available_skills and name not in available_skills:
        return _ActivationResolution(failure_message="Skill `/{name}` is not available for this agent.")

    skill_content = self._read_skill_content(skill_file, skills_root)
    content_hash = hashlib.sha256(skill_content.encode()).hexdigest()
    return _ActivationResolution(activation=_Activation(...))
```

**安全检查**：
- `_read_skill_content` 验证文件名必须是 `SKILL_MD`
- `resolve()` 检查文件路径在 `skills_root` 内（防止路径遍历）
- SHA256 哈希记录到审计日志，可追踪注入了什么版本的内容

#### 阶段 3：构造注入消息

注入的 XML 格式：

```xml
<slash_skill_activation>
The user explicitly activated the `graphify` skill for this turn.
Treat the task text as:
<user_request>
分析这个 URL
</user_request>

Follow this skill before choosing a general workflow.

<skill name="graphify" category="..." path="/mnt/skills/graphify/SKILL.md" sha256="abc123...">
<skill_content encoding="xml-escaped">
... 完整 SKILL.md 内容 ...
</skill_content>
</skill>
</slash_skill_activation>
```

**设计决策**：
- 用 `HumanMessage`（非 SystemMessage）注入——因为技能只在当轮生效，不应该持久化到系统 prompt
- `hide_from_ui: True` — 前端不显示这条注入消息
- `id=f"{target.id}__slash_activation"` — 与目标消息关联，支持幂等检查
- XML 转义（`html.escape`）— 防止技能内容中的 `<` `>` `&` 干扰 XML 结构

---

## 四、跨渠道适配

每种 IM 渠道对 `/command` 的处理不同，需要各自的适配逻辑：

| 渠道 | `/command` 处理 | 适配策略 |
|------|----------------|---------|
| **Web** | 无平台拦截 | 直接透传，前端 autocomplete 提示 |
| **Slack** | 有 `/command` 系统 | `command.text` 提取命令内容 |
| **Telegram** | 原生 `/command` | `message.text` 直传 |
| **Discord** | 有斜杠命令注册 | `interaction.data.resolved` 提取 |
| **飞书** | `/` 触发机器人 | 解析 event 文本 |
| **钉钉** | `/` 前缀消息 | 解析 message 文本 |
| **企微** | 无特殊处理 | 原始文本 |

**Channel Manager 的统一处理**：

```python
# manager.py 中的统一入口
async def _handle_slash_skill(self, text, ...):
    # 所有渠道最终调用同一个方法
    # 在发送到 agent 之前，保留原始 /skill-name 文本
    # SkillActivationMiddleware 在 agent 内部处理
```

关键：渠道层**只负责提取文本**，不做技能解析——解析统一由 middleware 完成，避免渠道间逻辑不一致。

---

## 五、前端集成

### Autocomplete

`input-box.tsx` 中实现 `/` 触发的技能名自动补全：

```
用户输入 "/" → 显示可用技能列表
用户选择 "/graphify" → 插入到输入框
用户继续输入 "分析这个 URL" → 发送 "/graphify 分析这个 URL"
```

### 消息隐藏

`message-list.tsx` 和 `utils.ts` 中过滤 `hide_from_ui: True` 的消息，不渲染注入的技能内容。

---

## 六、与其他技能加载方式对比

| 特性 | System Prompt 预加载 | Slash Skill 激活 |
|------|---------------------|-----------------|
| 加载时机 | Agent 初始化时 | 每轮 LLM 调用前 |
| 加载范围 | 所有 enabled 技能 | 仅用户指定的技能 |
| Token 消耗 | 每轮都消耗（所有技能） | 仅激活时消耗（单个技能） |
| 用户控制 | 无 | 显式 `/skill-name` |
| 适用场景 | 始终需要的基础技能 | 按需使用的专项技能 |
| 持久性 | 贯穿整个会话 | 仅当轮生效 |

---

## 七、关键设计决策

### 1. 注入 HumanMessage 而非 SystemMessage

技能内容只在当轮生效——如果用 SystemMessage，会随 LangGraph checkpoint 持久化到后续所有轮次，浪费 token 且可能干扰后续对话。

### 2. 幂等性保护

同一个用户消息不会被重复激活：
- 通过 `message.id` 关联：`activation_msg.id = f"{target.id}__slash_activation"`
- 通过 `additional_kwargs` 标记：`slash_skill_activation: True`
- 防止重放/流式重试导致重复注入

### 3. 失败即回复

技能不存在/禁用/不可用时，middleware 返回 `AIMessage(content=failure_message)` 直接告诉用户，而不是静默失败。

### 4. 审计追踪

每次激活都通过 `journal.record_middleware("skill_activation", ...)` 记录：
- 技能名、分类、路径、内容哈希
- 可回溯"哪一轮注入了哪个版本的什么技能"

---

## 八、文件索引

| 文件 | 职责 |
|------|------|
| `skills/slash.py` | 命令解析 + 保留名过滤 |
| `agents/middlewares/skill_activation_middleware.py` | 中间件：解析 + 验证 + 注入 |
| `agents/lead_agent/agent.py` | 注册 middleware 到 lead agent |
| `app/channels/manager.py` | IM 渠道统一入口 |
| `app/channels/commands.py` | Web 渠道命令处理 |
| `utils/messages.py` | `get_original_user_content_text` 提取原始文本 |
| `frontend/src/components/workspace/input-box.tsx` | 前端 autocomplete |
| `frontend/tests/e2e/chat.spec.ts` | E2E 测试 |
| `backend/tests/test_slash_skills.py` | 后端集成测试（557 行） |
| `backend/tests/test_channels.py` | 渠道适配测试（923 行） |
