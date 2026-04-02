# Skills 模块文件清单

## 模块概述

Skills 模块管理代理技能系统，支持技能发现、加载、解析和安装。

## 文件清单

### 1. `/data/deer-flow-main/backend/packages/harness/deerflow/skills/__init__.py`

**核心导出**:
- `load_skills()` - 加载所有技能
- `get_skills_root_path()` - 获取技能根目录
- `Skill` - 技能数据类
- `ALLOWED_FRONTMATTER_PROPERTIES` - 允许的 frontmatter 属性
- `_validate_skill_frontmatter()` - 验证技能 frontmatter
- `install_skill_from_archive()` - 从档案安装技能
- `SkillAlreadyExistsError` - 技能已存在错误

**职责**: Skills 模块的统一导出入口

---

### 2. `/data/deer-flow-main/backend/packages/harness/deerflow/skills/installer.py`

**核心类/函数**:
- `SkillAlreadyExistsError` - 技能已存在错误
- `is_unsafe_zip_member()` - 检查不安全的 ZIP 成员
- `is_symlink_member()` - 检测符号链接
- `should_ignore_archive_entry()` - 检查应忽略的条目
- `resolve_skill_dir_from_archive()` - 从档案解析技能目录
- `safe_extract_skill_archive()` - 安全提取技能档案
  - 拒绝绝对路径和目录遍历
  - 跳过符号链接
  - 强制大小限制
- `install_skill_from_archive()` - 安装 .skill 档案

**职责**: 技能档案安装逻辑（纯业务逻辑，无 FastAPI 依赖）

---

### 3. `/data/deer-flow-main/backend/packages/harness/deerflow/skills/loader.py`

**核心类/函数**:
- `get_skills_root_path()` - 获取技能根目录
- `load_skills()` - 加载所有技能
  - 扫描 public/custom 目录
  - 解析 SKILL.md
  - 从 extensions_config.json 读取启用状态
  - 支持按启用状态过滤

**职责**: 技能发现和加载

---

### 4. `/data/deer-flow-main/backend/packages/harness/deerflow/skills/parser.py`

**核心类/函数**:
- `parse_skill_file(skill_file, category, relative_path)` - 解析 SKILL.md
  - 提取 YAML frontmatter
  - 解析 name、description、license

**职责**: SKILL.md 文件解析

---

### 5. `/data/deer-flow-main/backend/packages/harness/deerflow/skills/types.py`

**核心类/函数**:
- `Skill` - 技能数据类
  - `name` - 技能名称
  - `description` - 描述
  - `license` - 许可证
  - `skill_dir` - 技能目录
  - `skill_file` - SKILL.md 文件
  - `relative_path` - 相对路径
  - `category` - 类别（public/custom）
  - `enabled` - 是否启用
  - `skill_path` - 技能路径
  - `get_container_path()` - 获取容器内路径
  - `get_container_file_path()` - 获取容器内文件路径

**职责**: 技能数据结构定义

---

### 6. `/data/deer-flow-main/backend/packages/harness/deerflow/skills/validation.py`

**核心类/函数**:
- `ALLOWED_FRONTMATTER_PROPERTIES` - 允许的 frontmatter 属性集合
- `_validate_skill_frontmatter(skill_dir)` - 验证技能 frontmatter
  - 检查必需字段（name, description）
  - 验证命名规范（hyphen-case）
  - 验证长度限制
  - 验证描述内容

**职责**: 技能验证逻辑（纯逻辑，无 HTTP 依赖）

---

## 技能目录结构

```
skills/
├── public/           # 公共技能（提交到仓库）
│   └── {skill}/
│       └── SKILL.md
└── custom/           # 自定义技能（不提交）
    └── {skill}/
        └── SKILL.md
```

## SKILL.md Frontmatter

```yaml
---
name: skill-name
description: One-line description
license: MIT
allowed-tools: tool1,tool2
author: author-name
version: 1.0.0
compatibility: deer-flow>=0.1.0
---
```
