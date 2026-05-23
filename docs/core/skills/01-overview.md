# 技能系统全局概览

> 技能（Skill）是 DeerFlow Agent 的可插拔扩展机制。每个技能是一个 Markdown 文件包（`SKILL.md` + 辅助文件），教会 Agent 如何执行特定领域的任务。

---

## 一、技能是什么

```
技能 = SKILL.md（元数据 + 指令） + 可选的辅助文件（references/templates/scripts/assets）
```

一个技能本质上是一份 **Agent 可读取的领域知识包**。Agent 通过 `read_file` 工具按需加载技能内容，然后按照技能中的指令执行任务。

**不是插件，是按需加载的参考手册** —— 技能不注入到 system prompt 中（仅技能名称和描述会注入），Agent 在需要时才读取完整内容。

---

## 二、解决的问题

| 问题 | 技能的解决方案 |
|------|---------------|
| **Agent 缺乏领域知识** | 技能提供结构化的最佳实践、框架、参考资源 |
| **重复性工作流没有标准化** | 技能封装成熟的"正确做法"，Agent 不必每次重新探索 |
| **提示词膨胀** | 按需加载（Progressive Loading）：仅注入技能列表，内容在需要时才读取 |
| **第三方无法扩展 Agent 能力** | `.skill` 归档包格式支持分享和安装 |
| **Agent 无法自我进化** | `skill_manage` 工具允许 Agent 在任务完成后自动创建/更新技能 |
| **多 Agent 场景下工具权限混乱** | `allowed-tools` 声明使每个技能可限制自己需要的工具 |

---

## 三、能力来源全景

```
┌────────────────────────────────────────────────────────────────────┐
│                         能力来源                                    │
├──────────────┬──────────────────────┬──────────────────────────────┤
│  内置技能     │  用户自定义技能        │  Agent 自创技能               │
│  (public/)   │  (custom/)           │  (通过 skill_manage 工具)      │
├──────────────┼──────────────────────┼──────────────────────────────┤
│ 平台捆绑      │ 用户手动安装/编写       │  Agent 在任务完成后自动创建     │
│ 只读         │ 可编辑、可删除          │ 受安全扫描约束                 │
│ 版本随平台更新 │ 版本独立               │ 记录在 custom/ 目录            │
└──────────────┴──────────────────────┴──────────────────────────────┘
```

能力注入路径：

```
skills/public/ 或 skills/custom/
    │
    ▼ 解析 (parser.py)
   Skill 对象列表
    │
    ├─► system prompt 注入 (prompt.py)
    │     └─ Agent 看到可用技能列表 → 按需 read_file 加载内容
    │
    ├─► 工具过滤 (tool_policy.py)
    │     └─ filter_tools_by_skill_allowed_tools() → 限制可用工具
    │
    └─► 沙箱挂载 (sandbox/tools.py)
          └─ bind mount 到容器 /mnt/skills/ → Agent 可读取
```

---

## 四、模块架构

```
skills/
├── __init__.py           # 模块入口，公开接口导出
├── types.py              # Skill 数据类、SkillCategory 枚举
├── parser.py             # SKILL.md 解析器（YAML frontmatter → Skill 对象）
├── validation.py         # 严格校验（交互式场景：上传、API 编辑）
├── installer.py          # .skill 归档包安装（安全解压、原子安装）
├── security_scanner.py   # LLM 驱动的安全审查
├── tool_policy.py        # 基于技能的工具白名单策略
└── storage/
    ├── __init__.py        # 存储工厂 + 单例管理
    ├── skill_storage.py   # 抽象基类（模板方法模式）
    └── local_skill_storage.py  # 本地文件系统实现
```

**上游消费者**：

| 消费者 | 使用的模块 | 用途 |
|--------|-----------|------|
| `agents/lead_agent/prompt.py` | `parser`, `storage` | 加载技能列表 → 注入 system prompt |
| `agents/lead_agent/agent.py` | `tool_policy` | 按技能声明过滤可用工具 |
| `tools/skill_manage_tool.py` | `storage`, `security_scanner` | Agent 自主创建/编辑/删除技能 |
| `subagents/executor.py` | `tool_policy`, `storage` | 子 Agent 独立加载技能和过滤工具 |
| `sandbox/tools.py` | `storage`, `types` | 技能路径解析 → 沙箱挂载 |
| Gateway API | `storage`, `validation` | 技能列表、安装、编辑、删除的 HTTP API |

---

## 五、数据流简图

```
用户对话开始
    │
    ├─ 构建 Agent (make_lead_agent)
    │   ├─ load_skills(enabled_only=True)     ← storage.load_skills()
    │   │   ├─ 遍历 public/ 和 custom/ 目录
    │   │   ├─ 解析每个 SKILL.md               ← parser.parse_skill_file()
    │   │   └─ 合并 extensions_config.json     ← 决定 enabled 状态
    │   │
    │   ├─ get_skills_prompt_section()         ← 生成 <skill_system> XML 块
    │   │   └─ 注入到 system prompt 中
    │   │
    │   └─ filter_tools_by_skill_allowed_tools() ← 按 allowed-tools 过滤工具
    │
    ├─ Agent 执行（ReAct 循环）
    │   ├─ 看到用户问题匹配某技能的 description
    │   ├─ 调用 read_file(<container_path>/SKILL.md)
    │   ├─ 加载技能的辅助资源（references/scripts/...）
    │   └─ 按技能指令完成任务
    │
    └─ 任务完成后（可选）
        └─ Agent 调用 skill_manage 工具
            ├─ 安全扫描                          ← security_scanner
            ├─ 写入 custom/ 目录                  ← storage.write_custom_skill()
            └─ 刷新 system prompt 缓存
```

---

## 六、与其他系统的关系

```
                    ┌──────────────┐
                    │  用户对话     │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ 记忆系统  │ │ 技能系统  │ │ MCP 系统 │
        │ 记住偏好  │ │ 领域知识  │ │ 外部工具  │
        └──────────┘ └──────────┘ └──────────┘
              │            │            │
              └────────────┼────────────┘
                           ▼
                  ┌────────────────┐
                  │  System Prompt │
                  │  (模板组装)     │
                  └────────────────┘
```

- **记忆系统** 回答"用户是谁"（偏好、背景）→ 个性化
- **技能系统** 回答"怎么做"（领域知识、工作流）→ 专业化
- **MCP 系统** 提供"用什么做"（外部工具、API）→ 工具化

三者独立运作但共用 `extensions_config.json` 进行启用/禁用管理。

---

## 七、关键设计原则

| 原则 | 说明 |
|------|------|
| **按需加载** | 不在 system prompt 中放入完整技能内容，Agent 按需 `read_file` |
| **软失败** | 损坏的 SKILL.md 不阻止 Agent 启动，仅跳过该技能 |
| **安全第一** | 安装前 LLM 安全扫描 + 保守回退策略（不确定就 block） |
| **原子操作** | 文件写入使用 temp + rename，安装使用三阶段提交 |
| **可替换存储** | 抽象基类 + 反射加载，支持切换到 PostgreSQL 等后端 |
| **Agent 可自进化** | `skill_manage` 工具让 Agent 在任务完成后创建/改进技能 |
