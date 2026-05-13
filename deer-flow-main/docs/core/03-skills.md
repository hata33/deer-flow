# Skills 技能系统——底层逻辑与本质

## 一句话本质

Skills 是 **比 Tools 更上层的业务能力合集**——一个 Skill 是一个 `SKILL.md` 文件，包含结构化元数据（YAML frontmatter）+ 非结构化知识（Markdown 正文）。Agent 按需加载，不是预装全部内容。

---

## 1. Markdown + YAML frontmatter——人类和 LLM 的最大公约数

```markdown
---
name: deep-research
description: Multi-step research methodology with source verification
license: MIT
allowed-tools: web_search, web_fetch, read_file
---

# Deep Research Workflow

## Step 1: Scope Definition
Identify the core question and sub-questions...

## Step 2: Source Discovery
Use web_search to find primary sources...
```

**为什么不用 JSON/Python/YAML 纯配置？** 因为 Skill 的核心是**长文本知识**——工作流指引、最佳实践、判断框架。这些内容在 Markdown 中写起来最自然，LLM 读起来最直接。frontmatter 提供结构化元数据（名称、描述、允许工具），正文提供非结构化知识。两者共存于同一文件，开发者用任何编辑器就能编写和预览。

**核心启示**：知识载体的格式选择要看"谁同时是生产者和消费者"。Skill 的生产者是开发者（写 Markdown），消费者是 LLM（读 Markdown）。Markdown 是两者理解能力的最大交集——不需要解析器、不需要序列化、不需要中间转换。

## 2. 渐进式加载——索引常驻 context，内容按需拉取

```
Agent 系统提示词中注入的内容（索引）：
┌─────────────────────────────────────────┐
│ <skill_system>                           │
│   <available_skills>                     │
│     <skill>                              │
│       <name>deep-research</name>         │
│       <description>Multi-step...</description> │
│       <location>/mnt/skills/public/deep-research/SKILL.md</location> │
│     </skill>                             │
│   </available_skills>                    │
│ </skill_system>                          │
└─────────────────────────────────────────┘

Agent 需要时才做的事：
  read_file("/mnt/skills/public/deep-research/SKILL.md")  ← 按需加载全文
  read_file("/mnt/skills/public/deep-research/refs/api.md") ← 按需加载子资源
```

**为什么不把 Skill 全文直接注入提示词？** 一个 Skill 可能有几千字的正文 + 引用的子资源。如果有 10 个 Skill，全部注入就是几万字的 token 消耗——大部分永远不会被用到。索引（名称 + 描述 + 路径）占用极小，Agent 判断需要时再 `read_file` 加载全文。

**核心启示**：这是操作系统"虚拟内存"的 Agent 版本——页表常驻内存（索引在 prompt 中），页面按需换入（内容通过工具调用加载）。不要把所有知识一股脑塞进 prompt，用"索引 + 按需加载"控制 token 预算。

## 3. public / custom 双目录——框架与用户的生命周期隔离

```
skills/
  ├─ public/              ← Git 跟踪，随仓库提交
  │   ├─ deep-research/
  │   └─ code-review/
  └─ custom/              ← .gitignore，用户自建或安装
      ├─ my-workflow/
      └─ team-standards/
```

`load_skills()` 统一扫描两个目录，对调用方透明。`install_skill_from_archive` 只写入 `custom/`，不触碰 `public/`。框架升级只动 `public/`，用户安装只动 `custom/`。

**核心启示**：把"平台提供的内容"和"用户生成的内容"放在不同的生命周期管理下。混在一起后，`git pull` 升级可能覆盖用户修改，或者 `.gitignore` 整个目录导致框架技能丢失。双目录让两者独立演进——和 Android 的 `/system/app`（系统应用）vs `/data/app`（用户安装）是同一模式。

## 4. 启用状态外置——能力注册与能力激活分离

```json
// extensions_config.json
{
  "skills": {
    "deep-research": { "enabled": true },
    "code-review": { "enabled": false },
    "my-workflow": { "enabled": true }
  }
}
```

Skill 的"存在"（文件在磁盘上）和"启用"（JSON 配置中的状态）是分离的。`load_skills()` 做两件事：(1) 从文件系统发现所有 Skill (2) 从 `extensions_config.json` 读取启用状态。两者在加载时合并，而不是在 Skill 文件中管理启用状态。

**为什么不在 SKILL.md 里加 `enabled: true/false`？** 因为启用状态是**运行时运维决策**，不是**Skill 本身的属性**。同一个 Skill 在开发环境启用、在生产环境禁用——如果把启用状态写进文件，不同环境需要不同的文件版本。外置到配置文件后，Skill 文件在所有环境完全一致，只有配置文件不同。

**核心启示**：区分"资源的定义"和"资源的状态"。定义是稳定的（Skill 的名称、描述、工作流不会变），状态是多变的（今天启用明天禁用）。把状态外置到独立配置，让定义可以安全地版本控制和分发。

## 5. 安装链的纵深防御——校验分散在每一步

`.skill` ZIP 安装流程：

```
文件校验（存在性 + 扩展名）
  → 安全解压（拒绝绝对路径、..遍历、符号链接、512MB 体积限制）
    → 路径二次校验（解压后验证无越界）
      → frontmatter 校验（名称格式、描述无尖括号）
        → 同名冲突检查（拒绝覆盖）
          → 复制到 custom/
```

每层只做自己的事，互不依赖。`is_unsafe_zip_member` 是纯函数，`_validate_skill_frontmatter` 也是纯函数，可独立测试。

**核心启示**：安全校验不要集中在一个"大检查"函数里。ZIP 归档是典型攻击面（路径遍历、符号链接、zip bomb、内容注入），威胁模型多样。分散校验让每一层可独立测试、独立演进。这和 Agent 系统中"提示词约束 + 思维引导 + 中间件截断"的三重纵深是同一模式——安全约束不要只靠一层。
