# 05 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **SKILL.md YAML frontmatter 标准化元数据** | 人类可读 + 机器可解析，Markdown 原生约定 |
| 2 | **allowed-tools 并集策略（任一声明即白名单模式）** | 防止安全降级——无限制技能不能绕过受限技能 |
| 3 | **技能在沙箱中只读挂载** | 防止运行时修改技能内容导致不可控行为 |
| 4 | **后台缓存预热（可选）** | 避免首次请求的冷启动延迟 |
| 5 | **静态 Prompt 注入（XML 标签）** | 利用 LLM prefix cache 复用，降低成本 |

---

## 二、逐决策分析

### 决策 1：SKILL.md YAML frontmatter

**问题**：技能元数据（名称、描述、许可证、工具白名单）用什么格式存储？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 单独 JSON/YAML 配置文件 | 格式严格，校验方便 | 两个文件需同步，维护成本高 |
| 目录名即名称，无元数据 | 最简单 | 无描述、无许可证、无工具策略 |
| SKILL.md YAML frontmatter（当前） | 一个文件包含元数据+内容，Markdown 原生约定 | YAML 解析有边界情况 |

**选择 SKILL.md frontmatter**：遵循 Markdown 社区约定（Jekyll、Hugo 等静态站点生成器），一个文件同时承载机器可读的元数据和人类可读的指令内容。`parse_skill_file()` 用正则 `^---\n(.*?)\n---\s*\n` 提取 frontmatter，`yaml.safe_load()` 解析为字典。

**字段白名单**：`ALLOWED_FRONTMATTER_PROPERTIES` 限定合法键为 `{name, description, license, allowed-tools, metadata, compatibility, version, author}`。超出白名单的键在校验阶段被拒绝——防止拼写错误和注入。

**软失败 vs 严格校验**：`parse_skill_file()`（发现阶段）返回 `None` 静默跳过坏技能；`_validate_skill_frontmatter()`（交互阶段）返回详细错误信息。发现阶段不阻塞 Agent 启动，交互阶段给用户明确反馈。

---

### 决策 2：allowed-tools 并集策略

**问题**：多个技能各自声明 `allowed-tools` 时，如何组合？

| 方案 | 行为 | 安全影响 |
|------|------|----------|
| 全部并集（所有技能的工具都允许） | 最大化可用工具 | 无限制技能把受限技能的限定打破 |
| 任一声明即白名单 + 未声明不贡献（当前） | 安全隔离 | 可能比预期更严格 |
| 交集（只保留共有的工具） | 最保守 | 可能没有任何工具可用 |

**选择"任一声明即白名单 + 未声明不贡献"**：一旦有任意技能声明了 `allowed-tools`，系统切换到白名单模式。未声明该字段的技能不向并集中贡献任何工具。

```python
# allowed_tool_names_for_skills() 核心逻辑
for skill in skills:
    if skill.allowed_tools is None:
        continue  # 未声明 → 不贡献
    has_explicit_declaration = True
    allowed.update(skill.allowed_tools)

if not has_explicit_declaration:
    return None  # 无人声明 → 不限制
return allowed    # 有人声明 → 白名单模式
```

**为什么这样安全**：技能 A 声明 `["read", "write"]`，技能 B 未声明。如果未声明的技能把所有工具都加回来，A 的限制形同虚设。当前策略下，未声明意味着"我不需要任何工具"而非"我需要所有工具"。

---

### 决策 3：沙箱中只读挂载

**问题**：技能文件在沙箱容器中如何挂载？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 读写挂载 | Agent 可修改技能 | 恶意提示词可能注入工具修改技能 |
| 只读挂载（当前） | 技能内容不可变，可审计 | Agent 无法在运行时更新技能 |

**选择只读挂载**：`PathMapping(container_path="/mnt/skills", read_only=True)`。技能内容在安装时经过 LLM 安全扫描（`scan_skill_content()`），运行时不应被篡改。Agent 通过 `SKILL.md` 路径引用技能指令，只读保证引用的确定性。

**public vs custom 分类**：`SkillCategory.PUBLIC`（平台内置，只读）和 `SkillCategory.CUSTOM`（用户安装，可编辑/删除）。public 技能不可修改——用户只能在 `custom/` 下创建同名技能来覆盖（`load_skills()` 中 custom 优先级高于 public）。

---

### 决策 4：后台缓存预热

**问题**：首次请求时加载技能需要递归扫描目录、解析 YAML、合并启用状态——可能增加延迟。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 完全按需加载 | 零启动开销 | 首次请求延迟 |
| 启动时全量加载（当前实现） | 首次请求无延迟 | 启动稍慢 |

**当前实现**：`load_skills()` 在 `make_lead_agent()` 构建时被调用。技能文件数量通常很少（< 50），加载时间可忽略。`_iter_skill_files()` 使用 `os.walk` 递归扫描 `public/` 和 `custom/` 目录，过滤隐藏目录（以 `.` 开头），效率足够。

**启用状态实时性**：`load_skills()` 每次调用都从 `ExtensionsConfig.from_file()` 重新读取启用状态，确保 Gateway API 的修改立即生效（与 MCP 的 mtime 策略一致）。

---

### 决策 5：静态 Prompt 注入

**问题**：技能指令如何进入 LLM context？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 动态注入（运行时 middleware） | 实时性最好 | 每次调用都重新构建，无 cache 复用 |
| 静态注入到 system prompt（当前） | prefix cache 复用，成本低 | 技能变更需重建 Agent |

**选择静态注入**：技能列表在 `apply_prompt_template()` 构建 system prompt 时注入，以 XML 标签包裹。技能变更后通过 `reset_agent()` 触发重建。技能变更频率远低于对话频率（天级 vs 秒级），延迟一轮可接受。

**注入格式**：

```xml
<available_skills>
  <skill name="code-review">
    <description>Code review skill for ...</description>
    <path>/mnt/skills/public/code-review/SKILL.md</path>
  </skill>
</available_skills>
```

**prefix cache 复用**：system prompt 在同一 Agent 实例的多次调用间保持不变（技能、模型配置不变时）。LLM 提供商的 prefix cache 可以跨请求复用，显著降低 token 成本和首 token 延迟。

---

## 三、实现效果

| 效果 | 实现方式 |
|------|----------|
| **零配置发现** | 递归扫描 skills/{public,custom}，有 SKILL.md 即为技能 |
| **安全的工具隔离** | allowed-tools 并集策略，防止安全降级 |
| **内容不可变** | 沙箱只读挂载 + LLM 安全扫描 |
| **热启用/禁用** | extensions_config.json 控制，每次 load_skills() 重新读取 |
| **安装安全** | ZIP 解压防护 + frontmatter 校验 + LLM 审查 + 原子安装 |
| **custom 覆盖 public** | 同名技能 custom 优先，支持用户定制 |
