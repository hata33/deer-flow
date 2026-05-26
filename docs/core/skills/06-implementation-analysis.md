# 06 - 技能系统实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/skills/` 目录下的源码，逐层拆解技能系统的实现细节。回答的是"代码怎么写的、为什么这么写"。

---

## 一、模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                      调用方（外部世界）                           │
│                                                                  │
│  agents/lead_agent/             app/gateway/routers/skills.py   │
│  ┌──────────────────────────┐   ┌──────────────────────────┐   │
│  │ apply_prompt_template()  │   │ install / update / list  │   │
│  │ get_available_tools()    │   │ delete / read             │   │
│  └────────────┬─────────────┘   └────────────┬─────────────┘   │
│               │                               │                  │
└───────────────┼───────────────────────────────┼──────────────────┘
                │                               │
┌───────────────▼───────────────────────────────▼──────────────────┐
│                      skills 包（内部世界）                        │
│                                                                   │
│  __init__.py ─── 统一导出入口                                      │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ storage/skill_storage.py ─── 抽象基类 + 模板方法            │  │
│  │   load_skills()  ─── 发现、解析、排序、过滤                  │  │
│  │   validate_skill_name()  ─── 连字符命名校验                  │  │
│  │   validate_relative_path()  ─── 防目录穿越                   │  │
│  └──────────┬─────────────────────────────────────────────────┘  │
│             │ 继承                                                │
│  ┌──────────▼─────────────────────────────────────────────────┐  │
│  │ storage/local_skill_storage.py ─── 本地文件系统实现          │  │
│  │   _iter_skill_files()  ─── os.walk 递归扫描                │  │
│  │   ainstall_skill_from_archive()  ─── ZIP 安装              │  │
│  │   delete_custom_skill()  ─── 删除 + 历史记录                │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ parser.py    │  │ types.py     │  │ tool_policy.py       │   │
│  │              │  │              │  │                      │   │
│  │ ① 解析      │  │ ② 数据类    │  │ ③ 工具策略           │   │
│  │ YAML 提取   │  │ Skill/Category│ │  并集过滤            │   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│                                                                   │
│  ┌──────────────────┐  ┌──────────────────────────────────────┐ │
│  │ validation.py    │  │ security_scanner.py                  │ │
│  │                  │  │                                      │ │
│  │ ④ 严格校验      │  │ ⑤ LLM 安全审查                      │ │
│  │ 白名单/命名/XSS │  │  allow/warn/block 判定              │ │
│  └──────────────────┘  └──────────────────────────────────────┘ │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ installer.py ─── ZIP 归档包安全安装                         │  │
│  │   safe_extract_skill_archive()  ─── 防穿越/炸弹/符号链接   │  │
│  │   _move_staged_skill_into_reserved_target()  ─── 原子安装  │  │
│  └────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

---

## 二、第 1 层：发现 — LocalSkillStorage._iter_skill_files()

### 2.1 目录遍历实现

```python
def _iter_skill_files(self) -> Iterable[tuple[SkillCategory, Path, Path]]:
    for category in SkillCategory:  # "public", "custom"
        category_path = self._host_root / category.value
        for current_root, dir_names, file_names in os.walk(category_path, followlinks=True):
            # 过滤隐藏目录（.history、.installing-* 等）
            dir_names[:] = sorted(name for name in dir_names if not name.startswith("."))
            if SKILL_MD_FILE not in file_names:
                continue
            yield category, category_path, Path(current_root) / SKILL_MD_FILE
```

**为什么用 `os.walk` 而非 `Path.rglob`**：`os.walk` 允许在遍历过程中修改 `dir_names` 列表来剪枝（过滤隐藏目录），`rglob` 不支持。

**遍历结果**：生成 `(category, category_root, skill_md_path)` 三元组，其中 `category_root` 用于计算技能的 `relative_path`（`md_path.parent.relative_to(category_root)`）。

### 2.2 模板方法：load_skills()

```
SkillStorage.load_skills(enabled_only=False)
      │
      ├─ ① _iter_skill_files() → 遍历所有 SKILL.md
      │
      ├─ ② parse_skill_file(md_path, category, relative_path)
      │    └─ 解析 YAML frontmatter → Skill 对象
      │    └─ 失败返回 None（静默跳过）
      │
      ├─ ③ 按 name 去重
      │    └─ skills_by_name[skill.name] = skill
      │    └─ custom 同名覆盖 public（遍历顺序：先 public 后 custom）
      │
      ├─ ④ 从 ExtensionsConfig 合并 enabled 状态
      │    └─ ExtensionsConfig.from_file()（每次重新读取）
      │    └─ is_skill_enabled(name, category)
      │
      ├─ ⑤ 按名称字母序排序
      │
      └─ ⑥ 可选过滤（enabled_only=True）
```

---

## 三、第 2 层：解析 — parser.py

### 3.1 parse_skill_file() 解析流程

```
输入：SKILL.md 文件路径
      │
      ├─ ① 检查文件存在 + 文件名为 SKILL.md
      │
      ├─ ② 正则提取 frontmatter
      │    └─ r"^---\s*\n(.*?)\n---\s*\n" (DOTALL)
      │
      ├─ ③ yaml.safe_load() 解析 YAML
      │
      ├─ ④ 提取必需字段：name, description
      │    └─ 缺失或非字符串 → return None
      │
      ├─ ⑤ 提取可选字段：license
      │
      ├─ ⑥ 解析 allowed-tools
      │    └─ None → 不限制
      │    └─ [] → 禁止所有工具
      │    └─ ["tool_a", "tool_b"] → 白名单
      │    └─ 非列表或含非字符串 → ValueError → return None
      │
      └─ ⑦ 构建 Skill 数据类
           └─ enabled=True（实际值由 ExtensionsConfig 合并）
```

### 3.2 allowed-tools 的三态语义

```python
def parse_allowed_tools(raw, skill_file):
    if raw is None:
        return None       # 未声明 → 不限制
    if not isinstance(raw, list):
        raise ValueError(...)
    # 显式列表（可能为空）
    return [item.strip() for item in raw if isinstance(item, str)]
```

| 值 | 含义 | 对工具策略的影响 |
|----|------|-----------------|
| `None`（字段不存在） | 不限制 | 白名单模式下不贡献任何工具 |
| `[]`（空列表） | 禁止所有工具 | 白名单模式下贡献零个工具 |
| `["bash", "read_file"]` | 只允许列出的工具 | 向白名单并集贡献这些工具 |

---

## 四、第 3 层：工具策略 — tool_policy.py

### 4.1 allowed_tool_names_for_skills()

```python
def allowed_tool_names_for_skills(skills: list[Skill]) -> set[str] | None:
    allowed: set[str] = set()
    has_explicit_declaration = False

    for skill in skills:
        if skill.allowed_tools is None:
            continue                    # 未声明 → 跳过
        has_explicit_declaration = True
        allowed.update(skill.allowed_tools)

    if not has_explicit_declaration:
        return None                     # 无人声明 → 不限制
    return allowed                      # 白名单集合
```

### 4.2 filter_tools_by_skill_allowed_tools()

```python
def filter_tools_by_skill_allowed_tools(tools, skills):
    allowed = allowed_tool_names_for_skills(skills)
    if allowed is None:
        return tools                    # 不限制 → 原样返回
    return [t for t in tools if t.name in allowed]  # 白名单过滤
```

**泛型设计**：`ToolT: NamedTool` 约束——任何有 `name: str` 属性的对象都满足 `NamedTool` Protocol，无需显式继承。

---

## 五、第 4 层：Prompt 注入

### 5.1 技能在 system prompt 中的表示

```
apply_prompt_template()
      │
      ├─ load_skills(enabled_only=True)
      │
      └─ 构建 XML 块注入 system prompt
           <available_skills>
             <skill name="code-review">
               <description>...</description>
               <path>/mnt/skills/public/code-review/SKILL.md</path>
             </skill>
           </available_skills>
```

### 5.2 沙箱挂载路径映射

```
宿主机路径                          容器路径
──────────────────                  ──────────────
skills/public/code-review/    →    /mnt/skills/public/code-review/
skills/custom/my-tool/        →    /mnt/skills/custom/my-tool/

Skill.get_container_path() = f"{container_base}/{category}/{skill_path}"
Skill.get_container_file_path() = f"{container_path}/SKILL.md"
```

**PathMapping**：`PathMapping(container_path="/mnt/skills", read_only=True)` 确保容器内技能文件只读。Agent 的 bash 工具不能修改技能内容。

---

## 六、第 5 层：安装安全链

### 6.1 ZIP 安装流程

```
ainstall_skill_from_archive(archive_path)
      │
      ├─ ① 校验文件存在 + .skill 扩展名
      │
      ├─ ② safe_extract_skill_archive(zf, tmp_path)
      │    ├─ is_unsafe_zip_member() → 拒绝绝对路径、..
      │    ├─ is_symlink_member() → 跳过符号链接
      │    └─ 总大小限制 512MB（防 zip 炸弹）
      │
      ├─ ③ resolve_skill_dir_from_archive()
      │    └─ 过滤 __MACOSX 和隐藏文件
      │    └─ 单目录包装 → 自动进入内层
      │
      ├─ ④ _validate_skill_frontmatter()
      │    └─ 白名单校验、命名规范、XSS 防护
      │
      ├─ ⑤ _scan_skill_archive_contents_or_raise()
      │    ├─ 先扫描 SKILL.md（最重要）
      │    ├─ 检测嵌套 SKILL.md（不允许）
      │    └─ scripts/ 下文件按可执行审查（更严格）
      │
      └─ ⑥ 原子安装（三阶段提交）
           ├─ staging 临时目录 ← copytree
           ├─ target.mkdir(mode=0o700) ← 预留目标
           ├─ 逐个移动文件到目标
           └─ 失败 → 清理目标目录
```

---

## 七、文件职责速查表

| 文件 | 代码行 | 核心职责 | 关键类/函数 |
|------|--------|----------|------------|
| `parser.py` | ~168 | YAML frontmatter 解析 | `parse_skill_file()`, `parse_allowed_tools()` |
| `types.py` | ~107 | 核心类型定义 | `Skill`, `SkillCategory` |
| `tool_policy.py` | ~107 | 工具白名单策略 | `allowed_tool_names_for_skills()`, `filter_tools_by_skill_allowed_tools()` |
| `validation.py` | ~129 | 严格交互式校验 | `_validate_skill_frontmatter()` |
| `security_scanner.py` | ~181 | LLM 安全审查 | `scan_skill_content()` |
| `installer.py` | ~305 | ZIP 归档包安全安装 | `safe_extract_skill_archive()`, `_move_staged_skill_into_reserved_target()` |
| `storage/skill_storage.py` | ~337 | 抽象基类 + 模板方法 | `SkillStorage`, `load_skills()` |
| `storage/local_skill_storage.py` | ~287 | 本地文件系统实现 | `LocalSkillStorage`, `_iter_skill_files()` |
