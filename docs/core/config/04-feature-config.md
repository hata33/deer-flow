# 功能配置

本篇覆盖配置系统中与 Agent 功能相关的配置：子代理、记忆、摘要、标题、工具、技能、Guardrails、循环检测。

## 一、子代理配置（subagents_config.py）

### 配置层级

```
1. 内置默认值（代码硬编码）
   ↓ 被覆盖
2. 全局配置（timeout_seconds, max_turns）
   ↓ 被覆盖
3. per-agent override（agents 字段中对应代理名的设置）
   ↓ 被覆盖
4. 自定义代理（custom_agents 中声明的全新代理类型）
```

### SubagentsAppConfig

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `timeout_seconds` | 900（15分钟） | 全局默认超时 |
| `max_turns` | None | 全局默认最大轮次（None=内置默认） |
| `agents` | {} | 按代理名的覆盖配置 |
| `custom_agents` | {} | 用户自定义代理类型 |

### per-agent 覆盖

```yaml
subagents:
  timeout_seconds: 900
  agents:
    general-purpose:
      timeout_seconds: 1800    # 覆盖全局超时
      model: gpt-4o-mini       # 使用不同模型
    bash:
      max_turns: 100           # 允许更多轮次
```

查询链：`override.field → 全局 field → 内置默认`

### 自定义代理

```yaml
subagents:
  custom_agents:
    code-reviewer:
      description: "Code review specialist"
      system_prompt: "You are a code reviewer..."
      tools: ["read_file", "bash"]
      disallowed_tools: ["task", "ask_clarification"]
      model: inherit
      max_turns: 50
      timeout_seconds: 900
```

默认禁止 `task`、`ask_clarification`、`present_files`，防止无限递归。

## 二、记忆配置（memory_config.py）

### 核心字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | true | 是否启用记忆提取 |
| `injection_enabled` | true | 是否注入到系统提示词 |
| `storage_path` | "" | 存储路径（空=按用户隔离） |
| `debounce_seconds` | 30 | 防抖间隔 |
| `max_facts` | 100 | 最大事实数 |
| `fact_confidence_threshold` | 0.7 | 最低存储置信度 |
| `max_injection_tokens` | 2000 | 注入最大 token 数 |

### 存储路径语义

| storage_path 值 | 实际路径 | 用户隔离 |
|----------------|----------|----------|
| `""`（空） | `{base_dir}/users/{user_id}/memory.json` | 是 |
| `/absolute/path` | `/absolute/path` | 否（共享） |
| `relative/path` | `{base_dir}/relative/path` | 取决于路径 |

## 三、摘要配置（summarization_config.py）

### 触发策略（trigger）

```yaml
summarization:
  trigger:
    type: messages
    value: 50
  # 或多条件（满足任一触发）
  trigger:
    - type: messages
      value: 50
    - type: fraction
      value: 0.8
```

| type | value 含义 | 示例 |
|------|-----------|------|
| `messages` | 消息条数 | 50 条后触发 |
| `tokens` | token 数量 | 4000 tokens 后触发 |
| `fraction` | 占最大输入的比例 | 80% 时触发 |

### 保留策略（keep）

```yaml
summarization:
  keep:
    type: messages
    value: 20    # 摘要后保留最近 20 条消息
```

### 技能保留

摘要时保留最近加载的 N 个技能文件，避免 Agent 丢失刚学习的技能：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `preserve_recent_skill_count` | 5 | 保留最近 N 个技能文件 |
| `preserve_recent_skill_tokens` | 25000 | 总 token 预算 |
| `preserve_recent_skill_tokens_per_skill` | 5000 | 单个技能文件上限 |
| `skill_file_read_tool_names` | [read_file, read, view, cat] | 被识别为技能读取的工具 |

## 四、标题配置（title_config.py）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | true | 是否启用 |
| `max_words` | 6 | 标题最大词数 |
| `max_chars` | 60 | 标题最大字符数 |
| `model_name` | None | 生成模型（None=默认模型） |
| `prompt_template` | 内置模板 | 生成提示词 |

## 五、工具配置（tool_config.py + tool_search_config.py）

### 工具声明

```yaml
tools:
  - name: bash
    group: sandbox
    use: deerflow.sandbox.tools:bash_tool
  - name: web_search
    group: search
    use: deerflow.community.tavily:tavily_tool
```

`use` 字段格式：`module.path:variable_name`，由 `reflection.resolve_variable()` 解析。

### 工具分组

```yaml
tool_groups:
  - name: sandbox
  - name: search
```

分组用于按需加载工具（如子代理只加载特定分组）。

### 工具搜索（延迟加载）

```yaml
tool_search:
  enabled: true    # MCP 工具不在上下文中加载，通过 tool_search 按需发现
```

## 六、技能配置（skills_config.py + skill_evolution_config.py）

### 目录定位

```yaml
skills:
  use: deerflow.skills.storage.local_skill_storage:LocalSkillStorage
  path: null              # 自动检测
  container_path: /mnt/skills
```

路径解析链：`path 字段 → DEER_FLOW_SKILLS_PATH → {project_root}/skills → 传统位置`

### 技能演化

```yaml
skill_evolution:
  enabled: false          # 默认关闭
  moderation_model_name: null  # 安全审核模型
```

## 七、Guardrails 配置（guardrails_config.py）

```yaml
guardrails:
  enabled: true
  fail_closed: true       # Provider 出错时阻止工具调用
  passport: null          # OAP 护照路径
  provider:
    use: deerflow.guardrails.builtin:AllowlistProvider
    config:
      allowed_tools: ["bash", "read_file"]
```

## 八、循环检测配置（loop_detection_config.py）

### 模式匹配

检测连续 N 次相同的工具调用集合：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `warn_threshold` | 3 | 重复 N 次后警告 |
| `hard_limit` | 5 | 重复 N 次后停止 |
| `window_size` | 20 | 追踪最近 N 次调用集合 |

### 频率检测

检测单个工具被调用的总次数：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `tool_freq_warn` | 30 | 调用 N 次后警告 |
| `tool_freq_hard_limit` | 50 | 调用 N 次后停止 |

### per-tool 覆盖

```yaml
loop_detection:
  tool_freq_overrides:
    bash:                  # bash 在批量操作中可能高频使用
      warn: 50
      hard_limit: 80
```

### 验证约束

```
hard_limit >= warn_threshold      # 必须先警告再停止
tool_freq_hard_limit >= tool_freq_warn
```
