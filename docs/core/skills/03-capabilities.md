# 运行时能力清单与来源

> 技能系统为 Agent 提供了哪些运行时能力，每项能力由哪个模块实现、依赖什么外部条件、如何配置。

---

## 能力全景矩阵

| # | 能力 | 模块 | 配置源 | 运行时依赖 |
|---|------|------|--------|-----------|
| 1 | 技能发现与加载 | `storage/skill_storage.py` → `load_skills()` | `skills_config.path` | 文件系统可读 |
| 2 | 技能列表注入 Prompt | `agents/lead_agent/prompt.py` → `get_skills_prompt_section()` | `skills_config.container_path` | 技能已加载 |
| 3 | 按需技能文件读取 | `read_file` 工具 | `skills_config.container_path` | 沙箱 bind mount |
| 4 | 基于技能的工具过滤 | `tool_policy.py` → `filter_tools_by_skill_allowed_tools()` | 技能 frontmatter `allowed-tools` | 技能已加载 |
| 5 | Agent 自主创建技能 | `tools/skill_manage_tool.py` → `_skill_manage_impl("create")` | `skill_evolution.enabled` | 安全扫描模型可用 |
| 6 | Agent 自主编辑技能 | `tools/skill_manage_tool.py` → `_skill_manage_impl("edit")` | 同上 | 同上 |
| 7 | Agent 增量修补技能 | `tools/skill_manage_tool.py` → `_skill_manage_impl("patch")` | 同上 | 同上 |
| 8 | Agent 删除技能 | `tools/skill_manage_tool.py` → `_skill_manage_impl("delete")` | 同上 | 技能存在 |
| 9 | 技能辅助文件管理 | `tools/skill_manage_tool.py` → `write_file/remove_file` | 同上 | 同上 |
| 10 | 安全审查 | `security_scanner.py` → `scan_skill_content()` | `skill_evolution.moderation_model_name` | LLM 可用 |
| 11 | 技能启用/禁用管理 | `extensions_config.py` → `ExtensionsConfig` | `extensions_config.json` | 配置文件可读 |
| 12 | 技能历史记录 | `storage/local_skill_storage.py` → `append/read_history()` | 无（自动） | custom/.history/ 目录可写 |
| 13 | 提示词缓存刷新 | `prompt.py` → `clear/refresh_skills_system_prompt_cache()` | 无（调用触发） | 无 |

---

## 一、技能发现与加载

### 能力描述

启动时扫描 `skills/public/` 和 `skills/custom/` 目录，发现所有包含 `SKILL.md` 的子目录，解析元数据并合并启用状态。

### 调用链

```
make_lead_agent()
  └─ get_skills_prompt_section()
      └─ get_enabled_skills_for_config(app_config)
          └─ get_or_new_skill_storage(app_config=...).load_skills(enabled_only=True)
              ├─ _iter_skill_files()           ← 抽象方法，文件系统实现
              │   └─ os.walk() 遍历 public/ 和 custom/
              ├─ parse_skill_file()            ← 解析每个 SKILL.md
              └─ ExtensionsConfig.from_file()  ← 合并 enabled 状态
```

### 配置

```yaml
skills:
  use: "deerflow.skills.storage.local_skill_storage:LocalSkillStorage"
  path: null              # null = 自动检测（项目根目录的 skills/）
  container_path: "/mnt/skills"
```

### 依赖

- 文件系统可读：`skills/public/` 和 `skills/custom/` 目录
- `extensions_config.json`：技能启用状态（可选，不存在时默认全部启用）
- `parser.py`：SKILL.md 的 YAML frontmatter 解析

---

## 二、技能列表注入 System Prompt

### 能力描述

将已启用的技能列表格式化为 XML 块，注入到 Agent 的 system prompt 中。Agent 在每次对话开始时就能看到可用的技能名称和描述。

### 注入格式

```xml
<skill_system>
You have access to skills that provide optimized workflows...

**Progressive Loading Pattern:**
1. When a user query matches a skill's use case, immediately call
   `read_file` on the skill's main file...

**Skills are located at:** /mnt/skills

<available_skills>
    <skill>
        <name>code-review</name>
        <description>Review code for bugs, style, and security issues</description>
        <location>/mnt/skills/public/code-review/SKILL.md</location>
    </skill>
</available_skills>
</skill_system>
```

### 过滤逻辑

```python
# 如果指定了 available_skills 白名单（子 Agent 场景）
if available_skills is not None:
    skills = [s for s in skills if s.name in available_skills]
```

### 缓存

- 技能列表按 `AppConfig` 对象 ID 缓存，避免每次构建 Agent 都重新扫描文件系统
- `skill_manage` 工具操作后自动刷新缓存
- 可通过 `clear_skills_system_prompt_cache()` 手动刷新

---

## 三、按需技能文件读取

### 能力描述

技能**内容**不注入 system prompt。Agent 通过 `read_file` 工具在运行时按需读取。

### 读取路径

```
Agent 调用: read_file("/mnt/skills/public/code-review/SKILL.md")
    │
    ├─ 沙箱内的文件系统调用
    │   └─ bind mount: 宿主机 skills/public/ → 容器 /mnt/skills/public/
    │
    └─ Skill.get_container_file_path() 计算路径
        └─ f"{container_base_path}/{category}/{skill_path}/SKILL.md"
```

### 路径解析

```python
# 宿主机路径解析（sandbox/tools.py）
def _resolve_skills_host_path(virtual_path: str, host_root: Path) -> Path:
    """将容器虚拟路径转换为宿主机文件系统路径。"""
    ...

# Skill 对象提供的路径方法
skill.get_container_path("/mnt/skills")   # → /mnt/skills/public/code-review
skill.get_container_file_path("/mnt/skills")  # → /mnt/skills/public/code-review/SKILL.md
```

---

## 四、基于技能的工具过滤

### 能力描述

技能可以在 frontmatter 中声明 `allowed-tools`，限制该技能场景下的可用工具集合。

### 策略规则

```
allowed_tool_names_for_skills(skills)
    │
    ├─ 无技能声明 allowed-tools？ → 返回 None（不限制）
    │
    └─ 有技能声明了 allowed-tools？
        └─ 取所有声明的并集（白名单模式）
            └─ 未声明的技能不贡献任何工具
```

### 语义三角

| `allowed-tools` 值 | 含义 |
|-------------------|------|
| `null`（字段未声明） | 该技能对工具无要求，但在白名单模式下不贡献工具 |
| `[]`（显式空列表） | 该技能明确禁止使用任何工具 |
| `["read_file", "grep_code"]` | 该技能仅需要这些工具 |

### 调用位置

```python
# agents/lead_agent/agent.py
tools = filter_tools_by_skill_allowed_tools(all_tools, skills)

# subagents/executor.py
tools = self._apply_skill_allowed_tools(skills)
```

### 为什么子 Agent 也独立过滤

子 Agent 有自己的技能配置（`config.skills` 白名单）。它们独立加载技能列表和过滤工具，确保子 Agent 的工具权限与其分配的技能匹配。

---

## 五、Agent 自主技能管理

### 能力描述

Agent 可以通过 `skill_manage` 工具创建、编辑、修补、删除自定义技能。这是技能系统"自我进化"能力的核心。

### 工具接口

```
skill_manage(action, name, content?, path?, find?, replace?, expected_count?)

action ∈ {create, edit, patch, delete, write_file, remove_file}
```

### 六种操作

| action | 功能 | 安全防护 |
|--------|------|---------|
| `create` | 创建新技能（SKILL.md） | frontmatter 校验 + 安全扫描 + 原子写入 |
| `edit` | 完全替换 SKILL.md 内容 | 同上 |
| `patch` | 精确替换指定文本片段 | `expected_count` 防多处误匹配 + 安全扫描 |
| `delete` | 删除整个技能目录 | 检查可编辑性 + 保存历史记录 |
| `write_file` | 写入辅助文件 | 路径安全校验 + 安全扫描 |
| `remove_file` | 删除辅助文件 | 路径安全校验 + 存在性检查 |

### 并发控制

```python
_lock = _get_lock(name)  # 按技能名称的 asyncio.Lock
async with lock:
    ...  # 同一技能的并发操作排队执行
```

### 触发条件（技能进化提示词）

```
Agent 考虑创建/更新技能的启发式规则：
- 任务使用了 5+ 次工具调用
- 遇到了非显而易见的错误或陷阱
- 用户纠正了方法且纠正后有效
- 发现了非平凡的、可复用的工作流
```

---

## 六、安全审查

### 能力描述

在技能安装和内容写入前，使用独立的 LLM 调用审查内容安全性。检查 prompt 注入、权限提升、数据泄露、不安全代码。

### 审查模型

```python
# security_scanner.py
model_name = config.skill_evolution.moderation_model_name
model = create_chat_model(name=model_name, thinking_enabled=False)
```

**设计考量**：
- 使用独立模型（与主对话 LLM 分离）→ 避免安全审查被攻击者操纵
- 关闭 thinking 模式 → 获得确定性的 JSON 输出
- 独立 system prompt（rubric）→ 审查标准不可注入

### 三种判定

| 判定 | 含义 | 对技能的影响 |
|------|------|------------|
| `allow` | 安全，放行 | 正常安装/写入 |
| `warn` | 边界情况（如外部 API 引用），放行但记录 | 正常安装/写入 |
| `block` | 明确危险，拒绝 | 抛出 `SkillSecurityScanError` |

### 保守回退

```
安全扫描失败的回退策略：
├─ LLM 有响应但无法解析 → block（要求人工审查）
├─ 可执行文件 + 审查不可用 → block
└─ 非可执行文件 + 审查不可用 → block
```

**宁可误拒，不可放过**。

---

## 七、技能启用/禁用管理

### 能力描述

通过 `extensions_config.json` 控制每个技能的启用状态，无需修改技能源文件。

### 配置文件格式

```json
{
  "skills": {
    "code-review": { "enabled": true },
    "old-skill": { "enabled": false }
  }
}
```

### 合并逻辑

```python
# ExtensionsConfig.is_skill_enabled()
skill_config = self.skills.get(skill_name)
if skill_config is None:
    # 配置文件中未声明 → 默认启用
    return skill_category in ("public", "custom")
return skill_config.enabled
```

**设计理由**：启用/禁用信息从技能源文件分离，使配置在技能升级后保留。

---

## 八、历史记录

### 能力描述

所有对 custom 技能的修改（创建、编辑、patch、删除、辅助文件变更）都记录在 JSONL 文件中。

### 存储格式

```
custom/.history/<name>.jsonl

{"ts": "2026-05-20T08:00:00Z", "action": "create", "author": "agent", "thread_id": "...", "file_path": "SKILL.md", "prev_content": null, "new_content": "...", "scanner": {"decision": "allow", "reason": "..."}}
{"ts": "2026-05-20T08:30:00Z", "action": "patch", "author": "agent", "thread_id": "...", "file_path": "SKILL.md", "prev_content": "...", "new_content": "...", "scanner": {"decision": "allow", "reason": "..."}}
```

### 用途

- 审计 Agent 自主修改了哪些技能
- 回滚到历史版本（通过 `prev_content`）
- 分析技能进化路径

---

## 配置总览

```yaml
# config.yaml
skills:
  use: "deerflow.skills.storage.local_skill_storage:LocalSkillStorage"
  path: null
  container_path: "/mnt/skills"

skill_evolution:
  enabled: true
  moderation_model_name: null  # null = 使用默认模型进行安全审查
```

```json
// extensions_config.json
{
  "skills": {
    "skill-name": { "enabled": true }
  }
}
```

```yaml
# SKILL.md frontmatter
---
name: my-skill
description: Does something useful
license: MIT
allowed-tools:
  - read_file
  - grep_code
---
```

---

## 文件索引

| 能力 | 实现文件 | 关键函数/类 |
|------|---------|------------|
| 发现与加载 | `storage/local_skill_storage.py` | `_iter_skill_files()`, `load_skills()` |
| Prompt 注入 | `agents/lead_agent/prompt.py` | `get_skills_prompt_section()` |
| 按需读取 | `sandbox/tools.py`, `types.py` | `get_container_file_path()` |
| 工具过滤 | `tool_policy.py` | `filter_tools_by_skill_allowed_tools()` |
| 技能管理 | `tools/skill_manage_tool.py` | `_skill_manage_impl()` |
| 安全审查 | `security_scanner.py` | `scan_skill_content()` |
| 启用管理 | `config/extensions_config.py` | `ExtensionsConfig.is_skill_enabled()` |
| 历史记录 | `storage/local_skill_storage.py` | `append_history()`, `read_history()` |
| 缓存管理 | `agents/lead_agent/prompt.py` | `clear_skills_system_prompt_cache()` |
