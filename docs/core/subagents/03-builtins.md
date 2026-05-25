# 内置代理

DeerFlow 提供两个开箱即用的内置子代理，覆盖了最常见的任务委派场景。每个内置代理都拥有针对特定场景优化的系统提示词、工具集和执行参数。

## general-purpose — 通用多步骤任务代理

### 定位

`general-purpose` 是默认的全能子代理，适用于需要同时进行探索和行动的复杂多步骤任务。它继承父代理的全部工具（除 task），拥有最大的灵活性。

### 配置

```python
GENERAL_PURPOSE_CONFIG = SubagentConfig(
    name="general-purpose",
    tools=None,                                          # 继承全部工具
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",                                     # 继承父代理模型
    max_turns=100,                                       # 支持复杂多步骤推理
)
```

### 适用场景

| 场景 | 说明 |
|------|------|
| 探索 + 修改 | 需要先了解代码结构再进行修改 |
| 复杂推理 | 需要多步推理来解释结果 |
| 多步骤依赖 | 多个步骤之间存在依赖关系 |
| 上下文隔离 | 任务较长，会消耗大量上下文窗口 |

### 系统提示词设计

`general-purpose` 的系统提示词强调：

1. **自主完成**：不向用户请求澄清，利用已有信息独立完成任务
2. **结构化输出**：返回包含摘要、发现、文件路径、问题和引用的结果
3. **高效执行**：逐步思考但果断行动
4. **引用格式**：使用 `[citation:Title](URL)` 格式标注外部来源

### 工具继承

`tools=None` 意味着 general-purpose 继承父代理的全部工具，包括：

```
sandbox tools:  bash, ls, read_file, write_file, str_replace, glob, grep
builtin tools:  present_files, ask_clarification, view_image
mcp tools:      从 extensions_config.json 加载的外部工具
community:      tavily, jina_ai, firecrawl, image_search
```

经过 `disallowed_tools` 过滤后，移除 `task`、`ask_clarification`、`present_files`。

## bash — 命令执行专家

### 定位

`bash` 子代理专注于在沙箱环境中执行一系列相关的命令行操作。它仅使用沙箱文件操作工具，系统提示词针对命令执行场景进行了优化。

### 配置

```python
BASH_AGENT_CONFIG = SubagentConfig(
    name="bash",
    tools=["bash", "ls", "read_file", "write_file", "str_replace"],  # 仅沙箱工具
    disallowed_tools=["task", "ask_clarification", "present_files"],
    model="inherit",                                                  # 继承父代理模型
    max_turns=60,                                                     # 支持多步骤命令序列
)
```

### 适用场景

| 场景 | 说明 |
|------|------|
| 关联命令序列 | 需要执行一系列相关的 bash 命令 |
| 终端操作 | git、npm、docker、pip 等终端工具操作 |
| 冗长输出 | 命令输出冗长，直接在主代理中执行会污染上下文 |
| 构建/测试/部署 | 编译、运行测试、部署等操作 |

### 不适用场景

- **简单单条命令**：应直接使用主代理的 bash 工具，无需委派
- **需要多种工具**：bash 代理只能使用文件操作工具，无法搜索网页或使用 MCP 工具
- **需要复杂推理**：bash 代理的系统提示词聚焦于命令执行，非通用推理

### 系统提示词设计

`bash` 的系统提示词包含四个关键部分：

#### 1. 执行指南

```
- 有依赖的命令逐条执行
- 独立的命令并行执行
- 同时报告 stdout 和 stderr
- 优雅处理错误并解释原因
- 谨慎执行破坏性操作（rm、覆盖等）
```

#### 2. 输出格式

每个命令或命令组的输出包含：
- 执行了什么命令
- 执行结果（成功/失败）
- 相关输出（冗长时进行摘要）
- 错误或警告信息

#### 3. 工作目录

```
- 用户上传: /mnt/user-data/uploads
- 用户工作空间: /mnt/user-data/workspace（默认工作目录）
- 输出文件: /mnt/user-data/outputs
- 自定义挂载: 部署配置的其他绝对容器路径
```

优先使用工作空间相对路径（如 `hello.txt`、`../uploads/input.csv`），仅在对自定义挂载目录操作时使用绝对路径。

## 工具过滤对比

两个内置代理的工具集对比：

| 工具 | general-purpose | bash |
|------|----------------|------|
| bash | 继承 | 包含 |
| ls | 继承 | 包含 |
| read_file | 继承 | 包含 |
| write_file | 继承 | 包含 |
| str_replace | 继承 | 包含 |
| glob | 继承 | 不包含 |
| grep | 继承 | 不包含 |
| present_files | 继承 | 不包含 |
| ask_clarification | 继承 | 不包含 |
| view_image | 继承 | 不包含 |
| MCP 工具 | 继承 | 不包含 |
| 社区工具 | 继承 | 不包含 |
| task | 禁止 | 禁止 |

**关键区别**：
- `general-purpose` 通过 `tools=None`（白名单未指定）继承全部工具，再通过 `disallowed_tools` 移除禁止项
- `bash` 通过 `tools=["bash", "ls", ...]`（显式白名单）限制为仅沙箱工具，再通过 `disallowed_tools` 移除禁止项（双重保障）

## 系统提示词设计哲学

### 自主执行原则

两个内置代理都遵循"自主执行"原则：
- 禁止 `ask_clarification`：子代理不应向用户提问，必须利用已有信息独立完成任务
- 禁止 `task`：防止子代理嵌套委派，避免无限递归
- 禁止 `present_files`：文件展示由主代理统一管理

### 输出格式规范

两个代理都定义了结构化的输出格式，确保返回给主代理的结果清晰可解析：
- `general-purpose`：摘要 + 发现 + 文件路径 + 问题 + 引用
- `bash`：执行的命令 + 结果 + 输出 + 错误

### 路径策略

两个代理都使用相同的虚拟路径系统（`/mnt/user-data/...`），并优先使用工作空间相对路径。绝对路径仅在操作部署配置的自定义挂载目录时使用。

## max_turns 差异

| 代理 | max_turns | 原因 |
|------|-----------|------|
| general-purpose | 100 | 复杂多步骤推理需要更多轮次 |
| bash | 60 | 命令执行步骤相对简单 |

`max_turns` 作为 `recursion_limit` 传入 LangChain Agent 的 `RunnableConfig`，超过此限制后 Agent 自动停止。
