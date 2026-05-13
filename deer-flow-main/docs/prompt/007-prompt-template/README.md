# 007-提示词模板模块

> 已验证来源：deer-flow 项目 `agents/lead_agent/prompt.py`
> 本提示词可在新项目中直接使用，通过适配层注入新项目的段落需求，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

Agent 的系统提示词不是固定文本——由多个动态段落按条件组装：角色、人格、记忆、技能、子代理策略、延迟工具等。
硬编码无法适应运行时参数变化（subagent_enabled、agent_name、tool_search）。
需要模板 + 条件注入机制。

**解决的核心痛点：**
- 提示词段落动态变化 → 模板占位符 + 空字符串禁用
- LLM 忽略并发限制 → 15+ 次重复强调
- 记忆占满上下文 → token 上限控制
- 技能文件过大 → 渐进加载
- 延迟工具节省 token → 只列名称不列 schema

---

## 二、输入契约

| 输入项 | 来源 | 说明 |
|--------|------|------|
| `subagent_enabled` | Agent 工厂 | 是否注入子代理编排段落 |
| `max_concurrent_subagents` | Agent 工厂 | 并发上限，填入模板 |
| `agent_name` | Agent 工厂 | 加载 SOUL.md 和记忆 |
| `available_skills` | Agent 工厂 | 可选的技能过滤 |

---

## 三、输出契约

```python
def apply_prompt_template(subagent_enabled=False, max_concurrent_subagents=3, *, agent_name=None, available_skills=None) -> str:
    """返回完整的系统提示词字符串。

    保证：
    - 禁用的段落不留空行
    - 并发上限在多处重复
    - 记忆不超 token 上限
    - 日期为当前日期
    """
```

### 段落注入规则

| 段落 | 条件 | 禁用时 |
|------|------|--------|
| soul | 有 SOUL.md | 空字符串 |
| memory | 启用且有数据 | 空字符串 |
| skills | 有已启用技能 | 空字符串 |
| deferred_tools | tool_search 启用且有延迟工具 | 空字符串 |
| subagent | subagent_enabled | 空字符串 |
| acp | 有 ACP 代理 | 空字符串 |
| date | 始终 | 无（始终注入） |

---

## 四、行为约束

### 约束 1：禁用段落返回空字符串

不返回 None 或带空行的字符串——模板 `.format()` 中空字符串自然消失。

### 约束 2：子代理并发限制必须重复强调

一次指令的遵循率低，从 HARD LIMIT / EXAMPLE / COUNTER-EXAMPLE / REMINDER 多角度重复。

### 约束 3：记忆注入有 token 上限

`max_injection_tokens` 防止记忆占满上下文窗口。

### 约束 4：技能渐进加载

提示词指导 LLM "先读主文件，按需加载引用"，不一次性注入全部内容。

### 约束 5：延迟工具只列名称

不列参数 schema——LLM 必须通过 tool_search 按需获取。

### 约束 6：SOUL.md 用 XML 标签包裹

`<soul>` 标签让 LLM 区分人格和系统指令。

---

## 五、验证场景

| # | Given | When | Then |
|---|-------|------|------|
| 1 | subagent_enabled=false | apply | 无 `<subagent_system>` |
| 2 | subagent_enabled=true | apply | 段落含正确的 max_concurrent |
| 3 | 无 SOUL.md | apply | 无 `<soul>` 标签 |
| 4 | 记忆禁用 | apply | 无 `<memory>` 标签 |
| 5 | tool_search 禁用 | apply | 无 `<available-deferred-tools>` |
| 6 | bash 子代理不可用 | apply | 示例用 read_file 而非 bash |
| 7 | 不同 agent_name | apply 两次 | SOUL 和记忆不同 |

---

## 六、自由度与禁区

### 可以改的

- 固定段落内容（thinking_style / clarification / citations 等）
- 模板引擎（str.format → Jinja2）
- 段落顺序
- 子代理编排策略描述
- 技能加载模式
- 引用格式

### 不能改的

- **禁用段落返回空字符串**：None 会导致 .format 报错
- **并发限制重复强调**：一次指令 LLM 几乎忽略
- **记忆 token 上限**：无限制会占满上下文
- **SOUL.md XML 标签**：无标签则 LLM 混淆人格和指令
- **延迟工具只列名称**：列 schema 则延迟加载无意义
- **日期注入**：LLM 训练数据有截止日期

---

## 七、依赖的上下游模块

```
[上游] 配置系统 → get_app_config(), get_memory_config()
[上游] 智能体配置 → load_agent_soul(agent_name)
[上游] 记忆系统 → get_memory_data(), format_memory_for_injection()
[上游] 技能系统 → load_skills(enabled_only=True)
[上游] 子代理系统 → get_available_subagent_names()
[上游] 工具系统 → get_deferred_registry()
[上游] ACP 系统 → get_acp_agents()
    ↓
[本模块] 提示词模板
    ↓
[下游] Agent 工厂 → apply_prompt_template(...) → 传给 create_agent
```
