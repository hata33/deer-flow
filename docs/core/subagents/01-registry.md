# 代理注册与发现

子代理注册系统管理所有可用的子代理配置，包括内置代理和用户自定义代理。它提供了代理发现、名称查找和配置覆盖等功能。

## SubagentConfig 数据类

`SubagentConfig` 是子代理系统的核心配置数据类，定义在 `config.py` 中：

```python
@dataclass
class SubagentConfig:
    name: str                           # 唯一标识符
    description: str                    # 功能描述（指导主代理何时委派）
    system_prompt: str | None = None    # 系统提示词
    tools: list[str] | None = None      # 工具白名单（None = 继承全部）
    disallowed_tools: list[str] | None  # 工具黑名单（默认 ["task"]）
    skills: list[str] | None = None     # 技能白名单（None = 继承全部）
    model: str = "inherit"              # 模型选择（"inherit" = 继承父代理）
    max_turns: int = 50                 # 最大代理轮次
    timeout_seconds: int = 900          # 执行超时（秒）
```

### 关键字段说明

**tools（工具配置）**：
- `None`：继承父代理的全部工具（general-purpose 使用此模式）
- `["bash", "ls", "read_file"]`：仅使用指定工具（bash 代理使用此模式）

**disallowed_tools（禁止工具）**：
- 始终包含 `"task"` 以防止子代理嵌套
- 可扩展禁止其他工具（如 `ask_clarification`、`present_files`）

**skills（技能配置）**：
- `None`：继承全部已启用技能
- `[]`：不加载任何技能
- `["skill-a", "skill-b"]`：仅加载指定技能

**model（模型配置）**：
- `"inherit"`：继承父代理的 LLM 模型
- 具体模型名称：使用指定的模型

### 模型名称解析

`resolve_subagent_model_name()` 按以下优先级解析模型名称：

```
config.model != "inherit"  →  使用 config.model
    ↓
parent_model is not None   →  继承父代理模型
    ↓
app_config 可用            →  使用配置文件第一个模型
    ↓
自动加载 app_config        →  get_app_config() 的第一个模型
```

## 代理发现 API

### list_subagents()

列出所有已注册的子代理配置（内置 + 自定义），每个配置已应用 config.yaml 覆盖：

```python
configs = list_subagents()
# [SubagentConfig(name="general-purpose", ...), SubagentConfig(name="bash", ...)]
```

### get_subagent_config()

按名称查找子代理配置，返回已应用覆盖的 `SubagentConfig`：

```python
config = get_subagent_config("bash")
if config:
    print(config.timeout_seconds)  # 可能被 config.yaml 覆盖
```

### get_available_subagent_names()

获取当前运行时应暴露给调用方的子代理名称。在沙箱限制场景下（主机 bash 不可用），自动隐藏 `bash` 子代理：

```python
names = get_available_subagent_names()
# ["general-purpose", "bash"]  ← 正常情况
# ["general-purpose"]          ← 主机 bash 不可用时
```

## 代理来源

### 内置代理

定义在 `builtins/__init__.py` 的 `BUILTIN_SUBAGENTS` 字典中：

```python
BUILTIN_SUBAGENTS = {
    "general-purpose": GENERAL_PURPOSE_CONFIG,
    "bash": BASH_AGENT_CONFIG,
}
```

### 自定义代理

从 `config.yaml` 的 `subagents.custom_agents` 段加载：

```yaml
subagents:
  enabled: true
  custom_agents:
    code-reviewer:
      description: "代码审查专家"
      system_prompt: "你是一个代码审查专家..."
      tools: ["bash", "read_file", "ls", "grep"]
      disallowed_tools: ["task"]
      max_turns: 30
      timeout_seconds: 600
```

`_build_custom_subagent_config()` 读取配置并转换为 `SubagentConfig` 实例。

## 配置覆盖机制

`get_subagent_config()` 实现了三层配置覆盖，镜像 Codex 的配置分层：

### 解析顺序

```
1. 查找内置代理定义
       ↓ 未找到
2. 查找 custom_agents 段
       ↓ 找到基础配置
3. 应用 config.yaml 覆盖
```

### 覆盖规则

| 配置项 | per-agent 覆盖 | 全局默认 | 适用范围 |
|--------|---------------|---------|---------|
| `timeout_seconds` | `agents.<name>.timeout_seconds` | `subagents.timeout_seconds` | 全局默认仅对内置代理生效 |
| `max_turns` | `agents.<name>.max_turns` | `subagents.max_turns` | 全局默认仅对内置代理生效 |
| `model` | `agents.<name>.model` | 无 | 仅 per-agent |
| `skills` | `agents.<name>.skills` | 无 | 仅 per-agent |

**重要**：全局默认值（`subagents.timeout_seconds`、`subagents.max_turns`）仅应用于内置代理，不会覆盖自定义代理在 `custom_agents` 段中定义的自身默认值。

### 覆盖示例

```yaml
subagents:
  enabled: true
  timeout_seconds: 1200        # 全局默认超时（仅影响内置代理）
  max_turns: 80                # 全局默认轮次（仅影响内置代理）
  agents:
    bash:
      timeout_seconds: 600     # bash 代理的特定超时覆盖
      max_turns: 40            # bash 代理的特定轮次覆盖
      model: "gpt-4o-mini"    # bash 代理使用更便宜的模型
```

解析结果：
- `general-purpose`：timeout=1200, max_turns=80（使用全局默认）
- `bash`：timeout=600, max_turns=40, model="gpt-4o-mini"（使用 per-agent 覆盖）

## 沙箱过滤

`get_available_subagent_names()` 根据 sandbox 配置过滤可用代理：

```python
def get_available_subagent_names():
    names = get_subagent_names()           # ["general-purpose", "bash"]
    if not is_host_bash_allowed():         # 检查沙箱配置
        names = [n for n in names if n != "bash"]  # 移除 bash 代理
    return names
```

这确保了在受限沙箱环境中，前端和 `task()` 工具仅暴露当前运行时可用的代理。
