# 扩展指南 — "我想加一个 X"

> 按任务组织的扩展手册：每个扩展点包含接口定义、示例实现、配置片段和步骤清单。

---

## 目录

1. [新工具](#1-新工具)
2. [新中间件](#2-新中间件)
3. [新 LLM Provider](#3-新-llm-provider)
4. [新记忆存储后端](#4-新记忆存储后端)
5. [新沙箱提供者](#5-新沙箱提供者)
6. [新 IM 频道](#6-新-im-频道)
7. [新技能](#7-新技能)
8. [新 MCP Server](#8-新-mcp-server)
9. [新子代理类型](#9-新子代理类型)
10. [新 Guardrail Provider](#10-新-guardrail-provider)

---

## 1. 新工具

### 接口

```python
from langchain.tools import tool

@tool("tool_name", parse_docstring=True)
def my_tool(param: str) -> str:
    """工具描述（LLM 会看到这段文字来决定何时使用）。

    Args:
        param: 参数说明。

    Returns:
        返回值说明。
    """
    ...
```

### 参考实现

| 工具 | 文件 | 特点 |
|------|------|------|
| Tavily 搜索 | `deerflow/community/tavily/tools.py` | 带配置读取 + 结果标准化 |
| Serper 搜索 | `deerflow/community/serper/tools.py` | API Key 回退到环境变量 |
| Jina 抓取 | `deerflow/community/jina_ai/tools.py` | 异步 + Readability 提取 |
| InfoQuest | `deerflow/community/infoquest/tools.py` | 三工具共享客户端 |

### 步骤

1. 在 `backend/packages/harness/deerflow/community/` 下创建目录（如 `my_tool/`）
2. 创建 `tools.py`，用 `@tool` 装饰器定义工具函数
3. 用 `get_app_config().get_tool_config("tool_name")` 读取配置
4. 在 `config.yaml` 中注册：
   ```yaml
   tools:
     - name: my_tool
       group: my_group
       use: deerflow.community.my_tool.tools:my_tool
       api_key: $MY_API_KEY
   ```
5. 运行 `make dev` 测试

---

## 2. 新中间件

### 接口

```python
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableConfig

class MyMiddleware(AgentMiddleware[ThreadState]):
    state_schema = ThreadState

    def before_agent(self, state, config):
        """Agent 执行前（如：注入上下文）"""
        return state

    def before_model(self, state, config):
        """LLM 调用前（如：压缩上下文）"""
        return state

    def wrap_model_call(self, state, request, config, *, call_next):
        """包裹 LLM 调用（如：错误重试）"""
        return call_next(state, request, config)

    def after_model(self, state, response, config):
        """LLM 调用后（如：token 统计）"""
        return response

    def wrap_tool_call(self, state, request, config, *, call_next):
        """包裹工具调用（如：安全审计）"""
        return call_next(state, request, config)

    def after_agent(self, state, config):
        """Agent 执行后（如：记忆更新）"""
        return state
```

### 钩子选择指南

| 钩子 | 适用场景 | 典型中间件 |
|------|---------|-----------|
| `before_agent` | 初始化资源、注入上下文 | ThreadData, Uploads, Sandbox |
| `before_model` | 修改发送给 LLM 的内容 | Summarization, ViewImage |
| `wrap_model_call` | 包裹 LLM 调用、错误处理 | LLMErrorHandling, DanglingToolCall |
| `after_model` | 处理 LLM 响应、统计 | TokenUsage, Title, SubagentLimit |
| `wrap_tool_call` | 拦截/审计工具调用 | Guardrail, Clarification, SandboxAudit |
| `after_agent` | 后处理、异步任务 | Memory |

### 步骤

1. 在 `backend/packages/harness/deerflow/agents/middlewares/` 创建文件
2. 继承 `AgentMiddleware[ThreadState]`，实现需要的钩子
3. 在 `agent.py` 的 `_build_middlewares()` 中注册（注意顺序）：
   ```python
   from deerflow.agents.middlewares.my_middleware import MyMiddleware
   middlewares.append(MyMiddleware())
   ```
4. `ClarificationMiddleware` 必须始终是最后一个
5. 如需条件加载，参考 `SubagentLimitMiddleware` 的 `if subagent_enabled:` 模式

---

## 3. 新 LLM Provider

### 接口

继承 LangChain 的 `BaseChatModel`（通常继承 `ChatOpenAI` 最省力）。

### 参考实现

`deerflow/models/vllm_provider.py` → `VllmChatModel`（子类化 `ChatOpenAI`，保留 vLLM 特有的 `reasoning` 字段）

### 步骤

1. 在 `backend/packages/harness/deerflow/models/` 创建文件
2. 继承 `ChatOpenAI` 或 `BaseChatModel`
3. 如需特殊字段处理，覆写 `_get_request_payload()` / `_stream_response_to_chat_generation_chunk()`
4. 在 `config.yaml` 中注册：
   ```yaml
   models:
     - name: my-model
       display_name: My Model
       use: deerflow.models.my_provider:MyChatModel
       model: my-model-name
       base_url: http://my-server:8000/v1
       api_key: $MY_API_KEY
       supports_thinking: false
       supports_vision: true
   ```
5. `use` 字段通过反射加载（`resolve_class`），无需修改工厂代码

---

## 4. 新记忆存储后端

### 接口

```python
from deerflow.agents.memory.storage import MemoryStorage

class MyMemoryStorage(MemoryStorage):
    def load(self, agent_name=None, *, user_id=None) -> dict:
        """加载记忆数据（带缓存）"""
        ...

    def reload(self, agent_name=None, *, user_id=None) -> dict:
        """强制重新加载（忽略缓存）"""
        ...

    def save(self, memory_data, agent_name=None, *, user_id=None) -> bool:
        """保存记忆数据"""
        ...
```

### 参考实现

`deerflow/agents/memory/storage.py` → `FileMemoryStorage`（JSON 文件存储，per-user 隔离）

### 步骤

1. 创建新类继承 `MemoryStorage`
2. 实现三个抽象方法
3. 在 `config.yaml` 中配置：
   ```yaml
   memory:
     enabled: true
     storage_class: deerflow.agents.memory.my_storage:MyMemoryStorage
   ```
4. 缓存键为 `(user_id, agent_name)` 元组，确保正确隔离

---

## 5. 新沙箱提供者

### 接口

两个抽象类：`SandboxProvider`（管理生命周期）+ `Sandbox`（执行操作）。

```python
from deerflow.sandbox.sandbox import Sandbox, SandboxProvider

class MySandboxProvider(SandboxProvider):
    def acquire(self, thread_id=None) -> str: ...   # 返回 sandbox_id
    def get(self, sandbox_id) -> Sandbox | None: ...
    def release(self, sandbox_id) -> None: ...

class MySandbox(Sandbox):
    def execute_command(self, command) -> str: ...
    def read_file(self, path) -> str: ...
    def write_file(self, path, content) -> None: ...
    def list_dir(self, path) -> str: ...
```

### 参考实现

| 实现 | 文件 | 特点 |
|------|------|------|
| Local | `deerflow/sandbox/local/` | PathMapping 翻译虚拟路径 |
| AIO | `deerflow/community/aio_sandbox/` | Docker 容器隔离 |

### 步骤

1. 实现 `SandboxProvider` 和 `Sandbox` 两个类
2. 支持虚拟路径约定（`/mnt/user-data/...`、`/mnt/skills/`）
3. 在 `config.yaml` 中注册：
   ```yaml
   sandbox:
     use: deerflow.sandbox.my_provider:MySandboxProvider
   ```

---

## 6. 新 IM 频道

### 接口

```python
from app.channels.base import Channel

class MyChannel(Channel):
    async def start(self) -> None: ...    # 启动监听
    async def stop(self) -> None: ...     # 优雅停止
    async def send(self, msg) -> None: ...  # 发送消息
```

### 参考实现

| 频道 | 文件 | 特点 |
|------|------|------|
| Slack | `app/channels/slack.py` | Socket Mode + runs.wait() |
| 飞书 | `app/channels/feishu.py` | 卡片原地更新 + runs.stream() |
| Telegram | `app/channels/telegram.py` | Long polling + runs.wait() |
| 钉钉 | `app/channels/dingtalk.py` | AI Card 流式更新 |

### 步骤

1. 在 `backend/app/channels/` 创建文件
2. 继承 `Channel`，实现 `start/stop/send`
3. 在 `config.yaml` 中配置：
   ```yaml
   channels:
     langgraph_url: http://localhost:8001/api
     my_channel:
       enabled: true
       api_key: $MY_CHANNEL_KEY
   ```
4. 注册到 `service.py` 的频道发现逻辑

**注意**: IM 频道在 `app/` 层，不在 `deerflow/` 层。遵循 harness/app 边界规则。

---

## 7. 新技能

### 格式

```
skills/custom/my-skill/
└── SKILL.md
```

```markdown
---
name: my-skill
description: "技能描述（LLM 会看到）"
allowed-tools:
  - bash
  - write_file
  - web_search
---

技能的详细说明内容...
```

### 步骤

1. 在 `skills/custom/` 下创建目录
2. 创建 `SKILL.md`，填写 YAML frontmatter（`name` 和 `description` 必填）
3. 可选添加 `allowed-tools` 限制可用工具
4. 技能自动被发现并挂载到沙箱的 `/mnt/skills/` 路径
5. 在 `extensions_config.json` 中控制启用状态：
   ```json
   { "skills": { "my-skill": { "enabled": true } } }
   ```

---

## 8. 新 MCP Server

### 配置

在 `extensions_config.json` 中添加：

```json
{
  "mcpServers": {
    "my-server": {
      "enabled": true,
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "my-mcp-server"],
      "env": { "API_KEY": "$MY_API_KEY" }
    }
  }
}
```

### 支持的 Transport

| 类型 | 必填字段 | 场景 |
|------|---------|------|
| `stdio` | `command` + `args` | 本地子进程 |
| `sse` | `url` + `headers` | 远程 SSE 服务 |
| `http` | `url` + `headers` | HTTP 流式 |

### 步骤

1. 编辑 `extensions_config.json`，添加服务器配置
2. 设置 `enabled: true`
3. MCP 工具在首次使用时自动加载（懒初始化 + mtime 缓存）
4. 通过 Gateway API `PUT /api/mcp` 也可运行时修改

---

## 9. 新子代理类型

### 配置

在 `config.yaml` 中定义：

```yaml
subagents:
  enabled: true
  custom_agents:
    my-agent:
      description: "专用子代理的描述"
      system_prompt: |
        你是一个专门做 X 的助手...
      tools: null                    # null = 继承全部工具
      disallowed_tools:
        - task                       # 禁止递归调用
        - ask_clarification
      model: gpt-4o                  # 可选，指定模型
      max_turns: 50                  # 最大轮次
```

### 步骤

1. 在 `config.yaml` 的 `subagents.custom_agents` 下添加定义
2. 设置 `description`（`task` 工具会展示给主代理选择）
3. 配置 `tools` 或 `disallowed_tools`
4. 主代理通过 `task(description="...", subagent_type="my-agent")` 调用

### 内置代理参考

| 代理 | 工具范围 | max_turns |
|------|---------|-----------|
| `general-purpose` | 全部（排除 task/ask_clarification/present_files） | 100 |
| `bash` | 仅 bash/ls/read_file/write_file/str_replace | 60 |

---

## 10. 新 Guardrail Provider

### 接口

```python
from deerflow.guardrails import GuardrailProvider, GuardrailRequest, GuardrailDecision

class MyProvider(GuardrailProvider):
    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """同步评估工具调用请求"""
        ...

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """异步评估工具调用请求"""
        ...
```

### 参考实现

`deerflow/guardrails/builtin.py` → `AllowlistProvider`（简单的允许/禁止列表）

### 步骤

1. 创建类实现 `GuardrailProvider` protocol
2. 实现 `evaluate()` 和 `aevaluate()`
3. 在 `config.yaml` 中配置：
   ```yaml
   guardrails:
     enabled: true
     fail_closed: true    # 评估失败时拒绝
     provider:
       use: deerflow.guardrails.my_provider:MyProvider
       config:
         my_param: value
   ```
4. `use` 字段通过反射加载

---

## 通用模式速查

### 读取配置

```python
from deerflow.config import get_app_config
config = get_app_config()
tool_config = config.get_tool_config("tool_name")
if tool_config and "key" in tool_config.model_extra:
    value = tool_config.model_extra["key"]
```

### 环境变量解析

config.yaml 中 `$VAR_NAME` 自动解析为 `os.getenv("VAR_NAME")`。

### 反射加载

```python
from deerflow.reflection import resolve_variable, resolve_class
tool_fn = resolve_variable("deerflow.community.my_tool.tools:my_tool")
MyClass = resolve_class("deerflow.my_module:MyClass", base_class=BaseClass)
```

### 虚拟路径

所有沙箱操作使用虚拟路径（`/mnt/user-data/...`），不要硬编码宿主路径。

### 错误处理

工具中返回错误信息字符串而非抛异常：
```python
return json.dumps({"error": "描述性错误信息"})
```
`ToolErrorHandlingMiddleware` 会捕获异常并转换为错误 ToolMessage。
