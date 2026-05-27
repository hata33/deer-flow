# 技能加载与运行时全链路

> 从 SKILL.md 文件发现到运行时工具过滤、沙箱挂载、按需加载的完整跨模块协作路径。

---

## 全链路架构图

```
┌──────────────┐  scan   ┌──────────────┐  parse  ┌──────────────┐  merge  ┌──────────────┐
│ File System  │ ──────▸ │ SkillStorage │ ──────▸ │  Extensions  │ ──────▸ │  Enabled     │
│ skills/      │         │ (discovery)  │         │  Config      │         │  Skills      │
│ {public,     │         └──────────────┘         └──────────────┘         └──────┬───────┘
│  custom}/    │                                                                  │
└──────────────┘                                                                  │
                                                                                  ▼
                             ┌────────────────────────────────────────────────────────────┐
                             │                    Agent Construction                      │
                             │  ┌────────────────┐  ┌─────────────────┐  ┌─────────────┐  │
                             │  │ System Prompt  │  │ Tool Policy     │  │ Sandbox     │  │
                             │  │ (skills XML)   │  │ (allowed-tools) │  │(/mnt/skills)│  │
                             │  └────────────────┘  └─────────────────┘  └─────────────┘  │
                             └───────────────────────────┬────────────────────────────────┘
                                                         │
                                                         ▼
                             ┌────────────────────────────────────────────────────────────┐
                             │                    Runtime Middleware                      │
                             │  ┌────────────────────┐  ┌───────────────────────────────┐ │
                             │  │ DynamicContext     │  │ DeferredToolFilterMiddleware  │ │
                             │  │ (memory + date)    │  │ (hide deferred tools)         │ │
                             │  └────────────────────┘  └───────────────────────────────┘ │
                             └────────────────────────────────────────────────────────────┘
```

---

## 阶段 ①：文件发现 — SkillStorage

**核心文件**: `packages/harness/deerflow/skills/storage/skill_storage.py` → `LocalSkillStorage`

**扫描范围**:
```
skills/
├── public/              # 公共技能（Git 管理）
│   ├── skill-a/
│   │   └── SKILL.md     # 技能定义文件
│   └── skill-b/
│       └── SKILL.md
└── custom/              # 自定义技能（Git 忽略）
    └── skill-c/
        └── SKILL.md
```

**SKILL.md 格式**:
```markdown
---
name: my-skill
description: "技能描述"
allowed-tools:
  - bash
  - write_file
  - web_search
---

技能的详细说明内容...
```

**发现流程**:
1. `LocalSkillStorage._iter_skill_files()` 递归遍历 `skills/public/` 和 `skills/custom/`
2. 每个 `SKILL.md` 通过 `parse_skill_file()` 解析
3. 提取 YAML frontmatter，验证必需字段（`name`、`description`）
4. 返回 `Skill` 对象列表

**跨模块协作**:
- **SkillStorage ↔ ExtensionsConfig**: 发现的技能列表将与启用/禁用状态合并

---

## 阶段 ②：启用状态合并 — ExtensionsConfig

**核心文件**: `packages/harness/deerflow/config/extensions_config.py`

**配置文件**: `extensions_config.json`
```json
{
  "skills": {
    "my-skill": { "enabled": true },
    "deprecated-skill": { "enabled": false }
  }
}
```

**状态判断逻辑**:
1. `ExtensionsConfig.from_file()` 加载配置
2. `is_skill_enabled(skill_name, skill_category)` 判断：
   - 公共技能：默认启用（`enabled=True`）
   - 自定义技能：默认启用，可单独禁用
3. 在 `SkillStorage.load_skills()` 中合并启用状态到每个 `Skill` 对象

**配置加载优先级**:
1. 显式 `config_path` 参数
2. `DEER_FLOW_EXTENSIONS_CONFIG_PATH` 环境变量
3. 项目根目录 `extensions_config.json`
4. 旧版 monorepo 路径

**跨模块协作**:
- **ExtensionsConfig ↔ SkillStorage**: 提供启用状态数据
- **ExtensionsConfig ↔ Gateway API**: `PUT /api/skills/{name}` 可运行时修改启用状态，触发 `reload_extensions_config()` 热重载

---

## 阶段 ③：系统提示词注入 — Prompt.py

**核心文件**: `packages/harness/deerflow/agents/lead_agent/prompt.py` → `apply_prompt_template()`

**注入格式**:
```xml
<skill_system>
You have access to skills... located at: /mnt/skills
<available_skills>
  <skill>
    <name>my-skill</name>
    <description>技能描述 [custom, editable]</description>
    <location>/mnt/skills/custom/my-skill/SKILL.md</location>
  </skill>
</available_skills>
</skill_system>
```

**缓存策略**:
1. `get_skills_prompt_section()` 生成技能提示词段落
2. 后台线程 `_refresh_enabled_skills_cache_worker()` 预热缓存
3. LRU 缓存避免重复生成格式化内容
4. 配置变更时自动失效缓存

**设计决策**:
- 系统提示词保持**静态**（不变），以利用 LLM 的前缀缓存（prefix cache）优化
- 技能列表作为系统提示词的一部分，在代理构建时一次性注入

**跨模块协作**:
- **Prompt ↔ SkillStorage**: 读取启用的技能列表
- **Prompt ↔ ExtensionsConfig**: 检查技能启用状态
- **Prompt ↔ Config**: 获取 `skills.container_path`（默认 `/mnt/skills`）

---

## 阶段 ④：工具策略过滤 — ToolPolicy

**核心文件**: `packages/harness/deerflow/skills/tool_policy.py`

**策略规则**:
```
情况 1: 没有任何技能声明 allowed-tools → 返回 None（所有工具可用）
情况 2: 至少一个技能声明了 allowed-tools → 取所有声明的并集
情况 3: 技能未声明 allowed-tools → 不贡献任何工具到并集（防止安全降级）
```

**过滤流程**:
1. `make_lead_agent()` 调用 `_load_enabled_skills_for_tool_policy()` 获取启用的技能
2. `allowed_tool_names_for_skills(skills)` 计算允许的工具名称集合
3. `filter_tools_by_skill_allowed_tools(tools, skills)` 执行实际过滤
4. 过滤后的工具列表传递给 LangChain Agent 创建

**示例**:
```python
# 技能 A 声明 allowed-tools: [bash, write_file]
# 技能 B 声明 allowed-tools: [bash, web_search]
# 最终可用: bash + write_file + web_search（并集）
```

**跨模块协作**:
- **ToolPolicy ↔ Agent Factory**: 在 `make_lead_agent()` 中调用过滤
- **ToolPolicy ↔ SkillsStorage**: 读取技能的 `allowed-tools` 元数据
- **ToolPolicy ↔ Tool Registry**: 过滤 `get_available_tools()` 返回的完整工具列表

---

## 阶段 ⑤：沙箱挂载 — Sandbox Provider

**核心文件**: `packages/harness/deerflow/sandbox/local/local_sandbox_provider.py` → `_setup_path_mappings()`

**挂载映射**:
```
容器路径 (Agent 视角):    /mnt/skills/
宿主路径 (实际位置):      deer-flow/skills/
权限:                     read-only
```

**挂载创建**:
```python
PathMapping(
    container_path=config.skills.container_path,  # "/mnt/skills"
    local_path=str(skills_path),                  # 宿主路径
    read_only=True,                               # 技能为只读
)
```

**每个线程的沙箱实例包含**:
- **共享静态映射**: `/mnt/skills` → skills 目录（只读）
- **动态线程映射**: `/mnt/user-data/{workspace,uploads,outputs}` → 线程专属目录

**AIO Docker 模式**:
- 技能目录通过 Docker volume 挂载到容器内的 `/mnt/skills`
- 代理在容器内使用相同的虚拟路径访问技能文件

**跨模块协作**:
- **SandboxProvider ↔ Config**: 读取 `skills.path` 和 `skills.container_path`
- **SandboxProvider ↔ ThreadDataMiddleware**: 共享线程目录结构

---

## 阶段 ⑥：动态上下文注入 — DynamicContextMiddleware

**核心文件**: `packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py`

**设计原理**:
系统提示词保持静态（技能列表固定），但运行时需要注入动态内容（记忆、日期）。`DynamicContextMiddleware` 解决了这个矛盾。

**注入方式**:
```xml
<!-- 作为第一条 HumanMessage 注入，保持系统提示词不变 -->
<system-reminder>
<memory>
  <facts>...</facts>
  <workContext>...</workContext>
</memory>
<current_date>2026-05-26, Monday</current_date>
</system-reminder>
```

**与技能系统的关系**:
1. 代理构建时，`get_enabled_skills_for_config()` 已被调用
2. 技能列表已在系统提示词中静态注入
3. `DynamicContextMiddleware` 不修改技能列表，只注入记忆和日期

**ID-swap 技术**:
- 动态注入的消息使用特殊 ID，在上下文压缩时可被识别和保护

**跨模块协作**:
- **DynamicContext ↔ Memory**: 读取用户记忆数据
- **DynamicContext ↔ SummarizationMiddleware**: 使用 `system-reminder` 标签保护动态内容不被压缩

---

## 阶段 ⑦：延迟工具过滤 — DeferredToolFilterMiddleware

**核心文件**: `packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py`

**适用场景**:
当工具列表过大时，部分工具被标记为"延迟加载"（deferred），不在初始 LLM 调用中暴露。

**两层过滤**:
1. **模型调用层** (`wrap_model_call()`): 从 `request.tools` 中移除延迟工具的 schema
2. **工具执行层** (`wrap_tool_call()`): 如果 LLM 仍然调用了延迟工具，返回错误

**工具发现机制**:
- 代理使用 `tool_search` 工具搜索和提升延迟工具
- 提升后的工具在后续调用中变为可见

**跨模块协作**:
- **DeferredToolFilter ↔ Tool Registry**: 知道哪些工具被标记为 deferred
- **DeferredToolFilter ↔ LLM Model**: 控制工具 schema 的可见性
- **DeferredToolFilter ↔ ToolPolicy**: 技能的 `allowed-tools` 可能包含延迟工具

---

## 跨模块交互总览

```
SkillsStorage          ExtensionsConfig         Gateway API
    │                       │                       │
    │ load_skills()         │ is_skill_enabled()    │ PUT /api/skills/{name}
    │                       │                       │
    └───────────┬───────────┘                       │
                │                                   │
                ▼                                   ▼
         Enabled Skills ◂────── reload_extensions_config()
                │
    ┌───────────┼───────────────────────┐
    │           │                       │
    ▼           ▼                       ▼
Prompt.py    ToolPolicy          Sandbox Provider
(注入XML)    (过滤工具)          (挂载 /mnt/skills)
    │           │                       │
    └───────────┼───────────────────────┘
                │
                ▼
         Agent Construction
         (make_lead_agent)
                │
                ▼
    ┌───────────┴───────────────┐
    │                           │
    ▼                           ▼
DynamicContextMiddleware   DeferredToolFilterMiddleware
(注入记忆+日期)            (隐藏延迟工具)
```

---

## 深入阅读

| 模块内文档 | 路径 |
|-----------|------|
| 技能系统总览 | `docs/core/skills/01-overview.md` |
| 技能生命周期 | `docs/core/skills/02-lifecycle.md` |
| 技能能力 | `docs/core/skills/03-capabilities.md` |
| 特性与策略 | `docs/core/skills/04-features-and-policies.md` |
| 配置系统 | `docs/core/config/` |
| 沙箱系统 | `docs/core/sandbox/` |
| 中间件系统 | `docs/core/agent/middlewares/` |
