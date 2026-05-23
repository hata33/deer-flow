# 技能完整生命周期

> 从技能诞生到消亡的完整过程：安装/创建 → 发现 → 加载 → 注入 → 运行时使用 → 更新 → 删除。每一步涉及哪些模块、做了什么决策、有哪些安全边界。

---

## 生命周期全景

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          技能生命周期                                         │
├──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬───────────┤
│ ① 来源    │ ② 发现   │ ③ 解析   │ ④ 注入   │ ⑤ 运行时  │ ⑥ 进化    │ ⑦ 消亡    │
│ 安装/创建 │ 目录扫描 │ 元数据提取│ 提示词组装│ 按需加载  │ 自主更新  │ 删除/卸载  │
├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼───────────┤
│installer │_iter_    │parser.py │prompt.py │read_file │skill_    │delete_    │
│.py       │skill_    │          │          │Tool      │manage    │custom_    │
│          │files()   │          │          │          │_tool     │skill()    │
└──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴───────────┘
```

---

## 阶段 ①：来源（技能从哪里来）

### 三种来源通道

```
来源通道
├── A. 平台内置 (public/)
│     └─ 打包在 DeerFlow 发布包中，随平台升级更新
│
├── B. 用户安装 (custom/)
│     ├─ B1. API 上传 .skill 归档包
│     │     └─ POST /api/skills/install → installer.ainstall_skill_from_archive()
│     ├─ B2. CLI 安装
│     │     └─ deerflow skill install <path/to/skill.skill>
│     └─ B3. 手动放入 custom/ 目录
│           └─ 直接创建 custom/<name>/SKILL.md
│
└── C. Agent 自创 (custom/)
      └─ Agent 调用 skill_manage(action="create") 工具
```

### 安装流程（.skill 归档包）

```
用户上传 /path/to/skill.skill
    │
    ├─ 1. 校验：文件存在 && 扩展名为 .skill
    ├─ 2. 解压到临时目录 (TemporaryDirectory)
    │     ├─ 安全防护：拒绝绝对路径、.. 穿越、符号链接
    │     └─ 安全防护：限制总解压大小 ≤ 512MB（zip 炸弹防御）
    ├─ 3. 定位技能根目录
    │     └─ 过滤 __MACOSX、.DS_Store，处理嵌套目录
    ├─ 4. 校验 frontmatter
    │     └─ _validate_skill_frontmatter()：name、description、命名规范
    ├─ 5. 安全扫描
    │     ├─ 扫描 SKILL.md（不可执行）
    │     └─ 扫描 scripts/ 下的文件（可执行，审查更严格）
    ├─ 6. 三阶段原子安装
    │     ├─ Stage 1: 复制到 staging 临时目录
    │     ├─ Stage 2: 创建目标目录（权限 0o700）
    │     └─ Stage 3: shutil.move 移动文件
    └─ 7. 刷新 system prompt 缓存
```

**为什么用三阶段提交**：如果第 6 步中途失败，`finally` 块会清理已创建的目标目录。确保安装要么完全成功，要么不留痕迹。

---

## 阶段 ②：发现（目录扫描）

### 扫描入口

```
SkillStorage.load_skills(enabled_only=bool)
    │
    └─ _iter_skill_files()                    ← 子类实现的抽象方法
        │
        └─ LocalSkillStorage._iter_skill_files()
            │
            ├─ 遍历 public/ 目录
            │   └─ os.walk() 递归查找 SKILL.md
            │       └─ 跳过隐藏目录（. 开头）
            │
            └─ 遍历 custom/ 目录
                └─ 同上
```

### 扫描特性

| 特性 | 说明 |
|------|------|
| **确定性** | `dir_names[:] = sorted(...)` 保证遍历顺序一致 |
| **跳过隐藏** | 过滤 `.` 开头的目录（`.history`、`.git` 等不会干扰） |
| **跟随符号链接** | `followlinks=True` 支持技能目录的符号链接 |
| **按需读取** | 只扫描文件名，不读取内容（解析在下一阶段） |

---

## 阶段 ③：解析（元数据提取）

### parser.parse_skill_file() 的决策树

```
parse_skill_file(skill_file, category, relative_path)
    │
    ├─ 文件不存在或不是 SKILL.md？ → return None（静默跳过）
    │
    ├─ 无 YAML frontmatter（不以 --- 开头）？ → return None
    │
    ├─ YAML 解析失败？ → 记录日志，return None
    │
    ├─ name 缺失/非字符串/为空？ → return None
    │
    ├─ description 缺失/非字符串/为空？ → return None
    │
    ├─ allowed-tools 格式错误？ → 记录日志，return None
    │
    └─ 全部通过 → 返回 Skill(name, description, license, ...)
```

**关键设计：软失败**。任何一个校验失败都返回 `None` 而非抛出异常。这确保单个损坏的技能不会阻止 Agent 启动。

### 解析结果

```python
Skill(
    name="code-review",           # 连字符命名
    description="Review code...",  # 人类可读描述
    license="MIT",                 # 可选
    skill_dir=Path("..."),         # 宿主机路径
    skill_file=Path("..."),        # SKILL.md 路径
    relative_path=Path("code-review"),  # 从分类根目录的相对路径
    category=SkillCategory.CUSTOM,      # public 或 custom
    allowed_tools=["read_file", "grep_code"],  # 工具白名单（可选）
    enabled=True,                  # 由 extensions_config.json 合并
)
```

---

## 阶段 ④：注入（System Prompt 组装）

### 注入策略：Progressive Loading

```
get_skills_prompt_section(available_skills)
    │
    ├─ load_skills(enabled_only=True)          ← 只加载启用的技能
    ├─ 过滤 available_skills（如果指定了白名单）
    ├─ 生成 XML 格式的技能列表
    │
    └─ 返回 <skill_system>...</skill_system>
```

### 注入到 system prompt 的内容

```xml
<skill_system>
You have access to skills that provide optimized workflows for specific tasks.

**Progressive Loading Pattern:**
1. When a user query matches a skill's use case, immediately call
   `read_file` on the skill's main file
2. Read and understand the skill's workflow
3. The skill file contains references to external resources
4. Load referenced resources only when needed
5. Follow the skill's instructions precisely

**Skills are located at:** /mnt/skills

<available_skills>
    <skill>
        <name>code-review</name>
        <description>Review code for bugs, style, and security issues</description>
        <location>/mnt/skills/public/code-review/SKILL.md</location>
    </skill>
    <!-- ... more skills ... -->
</available_skills>
</skill_system>
```

**注意**：只有技能**名称**和**描述**注入到 prompt，完整的技能内容不注入。Agent 按需通过 `read_file` 加载。

---

## 阶段 ⑤：运行时使用

### Agent 如何使用技能

```
用户: "帮我审查这段代码"
    │
    ├─ Agent 读取 system prompt → 看到可用技能列表
    ├─ Agent 匹配 "审查代码" → skill "code-review"
    │
    ├─ Agent 调用: read_file("/mnt/skills/public/code-review/SKILL.md")
    │   └─ 沙箱内文件系统读取（bind mount）
    │
    ├─ Agent 理解技能指令
    │   └─ "先检查安全问题，再检查风格，最后检查性能..."
    │
    ├─ Agent 按需加载辅助资源
    │   ├─ read_file("/mnt/skills/public/code-review/references/checklist.md")
    │   └─ read_file("/mnt/skills/public/code-review/templates/report.md")
    │
    └─ Agent 执行任务并返回结果
```

### 运行时涉及的能力

| 能力 | 模块 | 说明 |
|------|------|------|
| 技能列表可见性 | `prompt.py` → `<skill_system>` | Agent 知道有哪些技能及其描述 |
| 技能文件读取 | `read_file` 工具 + 沙箱挂载 | Agent 按需读取 SKILL.md 和辅助文件 |
| 工具白名单限制 | `tool_policy.py` | 如果技能声明了 `allowed-tools`，过滤工具列表 |
| 技能自管理 | `skill_manage_tool` | Agent 可创建/编辑/删除自定义技能 |

---

## 阶段 ⑥：进化（Skill Evolution）

### Agent 自主创建技能

```
任务完成后
    │
    ├─ Agent 判断是否需要创建技能（技能进化提示词的启发式规则）
    │   ├─ 任务使用了 5+ 次工具调用？
    │   ├─ 遇到了非显而易见的错误或陷阱？
    │   ├─ 用户纠正了方法且纠正后有效？
    │   └─ 发现了非平凡的、可复用的工作流？
    │
    ├─ Agent 调用 skill_manage(action="create", name="...", content="...")
    │   ├─ 1. 检查同名技能不存在
    │   ├─ 2. validate_skill_markdown_content() → 校验 frontmatter
    │   ├─ 3. scan_skill_content() → LLM 安全审查
    │   ├─ 4. write_custom_skill() → 原子写入文件
    │   ├─ 5. append_history() → 记录历史
    │   └─ 6. refresh_skills_system_prompt_cache_async() → 刷新缓存
    │
    └─ 下一轮对话 → 新技能出现在可用列表中
```

### 技能增量更新（patch）

```
Agent 使用某技能时发现不足
    │
    ├─ Agent 调用 skill_manage(action="patch", name="...", find="...", replace="...")
    │   ├─ 读取现有 SKILL.md → 精确替换指定文本
    │   ├─ 可选 expected_count：指定预期替换次数（防止意外多处匹配）
    │   ├─ 安全扫描新内容
    │   └─ 原子写入 + 历史记录 + 刷新缓存
    │
    └─ 下次使用该技能 → 已是改进后的版本
```

**为什么推荐 patch 而非 edit**：patch 只替换指定片段，降低 Agent 意外破坏技能其他部分的风险。`expected_count` 参数进一步增加安全性。

---

## 阶段 ⑦：消亡（删除/卸载）

### 删除流程

```
delete_custom_skill(name, history_meta)
    │
    ├─ 1. validate_skill_name() → 校验名称格式
    ├─ 2. ensure_custom_skill_is_editable()
    │     ├─ custom 技能存在？ → 继续
    │     ├─ 同名的 public 技能存在？ → 报错（内置技能不可删除）
    │     └─ 都不存在？ → FileNotFoundError
    │
    ├─ 3. 保存历史记录（可选）
    │     └─ append_history() → custom/.history/<name>.jsonl
    │         └─ 写入失败（权限不足）不阻止删除
    │
    └─ 4. shutil.rmtree(target) → 递归删除整个技能目录
```

### 删除后的影响

| 影响 | 说明 |
|------|------|
| **提示词自动更新** | 缓存刷新后，下一轮对话的技能列表不包含已删除的技能 |
| **历史记录保留** | 删除前的 `prev_content` 保存在 `.history/<name>.jsonl` 中 |
| **占用的工具权限释放** | `allowed-tools` 列表中该技能的声明不再生效 |

---

## 缓存策略

```
技能列表缓存层级
    │
    ├─ L1: 进程级缓存 (_enabled_skills_cache)
    │     └─ 首次加载后缓存，手动调用 clear_skills_system_prompt_cache() 清除
    │
    ├─ L2: 配置级缓存 (_enabled_skills_by_config_cache)
    │     └─ 按 AppConfig 对象 ID 缓存，不同 config 有独立缓存
    │
    └─ 缓存失效触发点
          ├─ skill_manage 工具执行 create/edit/patch/delete 后自动刷新
          ├─ Gateway API 的技能 CRUD 操作后手动调用
          └─ 调用 reset_skill_storage() → 清除存储单例 + 技能缓存
```

**为什么需要多级缓存**：`load_skills()` 每次调用都要遍历文件系统和读取 `extensions_config.json`。在频繁创建 Agent 的场景下（如每次用户请求），缓存避免了重复 I/O。

---

## 文件索引

| 阶段 | 核心文件 | 关键函数 |
|------|---------|---------|
| ① 来源 | `installer.py` | `ainstall_skill_from_archive()`, `safe_extract_skill_archive()` |
| ② 发现 | `storage/local_skill_storage.py` | `_iter_skill_files()` |
| ③ 解析 | `parser.py` | `parse_skill_file()`, `parse_allowed_tools()` |
| ④ 注入 | `agents/lead_agent/prompt.py` | `get_skills_prompt_section()`, `apply_system_prompt_template()` |
| ⑤ 运行时 | `tool_policy.py` | `filter_tools_by_skill_allowed_tools()` |
| ⑥ 进化 | `tools/skill_manage_tool.py` | `_skill_manage_impl()` |
| ⑦ 消亡 | `storage/local_skill_storage.py` | `delete_custom_skill()` |
| 缓存 | `agents/lead_agent/prompt.py` | `clear_skills_system_prompt_cache()`, `_get_enabled_skills()` |
