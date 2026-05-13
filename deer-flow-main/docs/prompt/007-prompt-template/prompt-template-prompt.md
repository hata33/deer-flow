# 007-提示词模板

## 解决什么问题

Agent 的系统提示词不是固定文本——它由多个动态段落按条件组装：角色定义、人格（SOUL.md）、记忆上下文、技能列表、子代理编排策略、延迟工具列表、ACP 代理说明、工作目录结构、引用格式等。
硬编码任何一段都无法适应运行时参数变化（subagent_enabled、agent_name、tool_search 等）。
本模块把提示词定义为带占位符的模板，运行时按条件填充。

## 本模块的职责边界

**只负责提示词组装**：从各来源收集动态段落，填充模板占位符，返回完整字符串。
不负责：提示词发送给 LLM（Agent Loop 的事）、段落内容的生成逻辑（记忆/技能等模块的事）。

## 不可变的设计决策

**模板用 Python f-string 占位符，非 Jinja2**：`SYSTEM_PROMPT_TEMPLATE.format(...)`。
Jinja2 增加依赖且模板引擎功能在此场景无用——占位符都是简单的字符串替换，没有循环/条件/继承。

**段落按条件注入，空字符串表示禁用**：
- `subagent_section`：`subagent_enabled=True` 时注入完整编排策略，False 时空字符串。
- `skills_section`：无技能时空字符串。
- `deferred_tools_section`：tool_search 未启用或无延迟工具时空字符串。
- `acp_section`：无 ACP 代理时空字符串。
- `memory_context`：记忆未启用或无数据时空字符串。
空字符串在模板中自然消失，不留多余空行。

**SOUL.md 与结构化配置分离**：人格文本（SOUL.md）是 Markdown 格式，需要频繁编辑和自由排版，不适合嵌在 YAML 字符串中。用 `<soul>` XML 标签包裹注入，与系统提示词的其他结构化段落区分。

**子代理段落重复强调并发限制**：`_build_subagent_section` 中 `{n}` 出现 15+ 次——HARD LIMIT、USAGE EXAMPLE、COUNTER-EXAMPLE、CRITICAL REMINDER。
原因：LLM 对"最多 N 个"的指令遵循率低，需要从多个角度反复强调。只说一次的指令几乎被忽略。

**子代理段落的 bash 可用性检测**：`"bash" in get_available_subagent_names()` 动态决定示例代码用 `bash("npm test")` 还是 `read_file(...)` 作为"直接执行"的反例。
不可用时还提示用户切换到 AioSandboxProvider——这是沙箱安全策略的延伸。

**记忆上下文用 XML 标签包裹**：`<memory>...</memory>`。XML 标签让 LLM 明确知道这是记忆信息而非当前对话内容。没有标签则 LLM 可能混淆记忆和历史消息。

**记忆注入有 token 上限**：`max_tokens=config.max_injection_tokens`。无限制的话记忆可能占满上下文窗口，留给工具调用和响应的空间不足。

**技能用渐进加载模式**：提示词告诉 LLM "先 read_file 技能主文件，按需加载引用资源"。不一次性加载全部技能内容——技能文件可能很大，全部注入浪费 token。

**延迟工具只列名称**：`<available-deferred-tools>` 只列出工具名称，不列出参数 schema。LLM 知道工具存在但无法直接调用，必须通过 `tool_search` 获取完整定义。这是延迟加载策略的提示词侧体现。

**当前日期注入尾部**：`<current_date>2025-01-15, Wednesday</current_date>`。LLM 的训练数据有截止日期，注入当前日期避免过时信息。

**apply_prompt_template 是唯一入口**：所有段落收集和模板填充集中在一个函数。Agent 工厂只需调用 `apply_prompt_template(subagent_enabled, max_concurrent, agent_name=...)`。

## 适配层

```yaml
<ADAPT>
# === 模板引擎 ===
template_engine: "python str.format"  # 或 jinja2 / mustache

# === 动态段落（按需启用）===
sections:
  - name: "role"
    placeholder: "{agent_name}"
    source: "参数传入"
  - name: "soul"
    placeholder: "{soul}"
    source: "load_agent_soul(agent_name) → SOUL.md"
    condition: "agent_name 有 SOUL.md"
  - name: "memory"
    placeholder: "{memory_context}"
    source: "get_memory_data(agent_name)"
    condition: "memory.enabled && memory.injection_enabled"
    config: "max_injection_tokens"
  - name: "skills"
    placeholder: "{skills_section}"
    source: "load_skills(enabled_only=True)"
    condition: "有已启用的技能"
    pattern: "渐进加载（先读主文件，按需读引用）"
  - name: "deferred_tools"
    placeholder: "{deferred_tools_section}"
    source: "DeferredToolRegistry.entries"
    condition: "tool_search.enabled && 有延迟工具"
    pattern: "只列名称，不列 schema"
  - name: "subagent"
    placeholder: "{subagent_section}"
    source: "_build_subagent_section(max_concurrent)"
    condition: "subagent_enabled == true"
    repeats_limit: "15+ 次强调并发上限"
  - name: "acp"
    placeholder: "{acp_section}"
    source: "get_acp_agents()"
    condition: "有配置的 ACP 代理"
  - name: "date"
    source: "datetime.now()"
    position: "尾部"
    format: "YYYY-MM-DD, Weekday"

# === 固定段落 ===
fixed_sections:
  - "thinking_style"
  - "clarification_system"
  - "working_directory"
  - "response_style"
  - "citations"
  - "critical_reminders"
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | subagent_enabled=false | 无 `<subagent_system>` 段落 |
| 2 | subagent_enabled=true | 段落中 max_concurrent 值正确 |
| 3 | 无 SOUL.md | `{soul}` 为空字符串，不留空行 |
| 4 | 记忆禁用 | `{memory_context}` 为空 |
| 5 | 记忆启用但无数据 | `{memory_context}` 为空 |
| 6 | 无技能 | `{skills_section}` 为空 |
| 7 | tool_search 禁用 | `{deferred_tools_section}` 为空 |
| 8 | 无 ACP 代理 | `{acp_section}` 为空 |
| 9 | bash 子代理不可用 | 示例用 read_file 而非 bash |
| 10 | 两次调用不同 agent_name | SOUL 和记忆不同 |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **配置系统** | `get_app_config()` / `get_memory_config()` |
| **智能体配置** | `load_agent_soul(agent_name)` |
| **记忆系统** | `get_memory_data()` / `format_memory_for_injection()` |
| **技能系统** | `load_skills(enabled_only=True)` |
| **子代理系统** | `get_available_subagent_names()` |
| **工具系统** | `get_deferred_registry()` (延迟工具名称) |
| **ACP 系统** | `get_acp_agents()` |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `prompt.py` | 完整提示词模板系统 | `SYSTEM_PROMPT_TEMPLATE` 的 XML 标签分段结构；`apply_prompt_template` 的段落收集 + 条件注入；`_build_subagent_section` 的 15+ 次并发限制重复强调；`_get_memory_context` 的 token 上限；`get_skills_prompt_section` 的渐进加载模式；`get_deferred_tools_prompt_section` 的名称列表 vs 完整 schema |

源码文件见同目录下的 `src/` 子文件夹。
