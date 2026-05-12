# 工具加载启示

> 来源：`backend/packages/harness/deerflow/tools/tools.py`、`reflection/resolvers.py`、`tools/builtins/tool_search.py`

## 1. 声明式工具注册 + 反射加载——配置与代码彻底解耦

`config.yaml` 中的工具条目只记录 `"use": "deerflow.sandbox.tools:bash_tool"` 这样的字符串路径，`resolve_variable` 在运行时通过 `importlib` 动态导入并验证类型。工具的增删完全不需要改代码，只需编辑配置。

- **反射机制**：`resolve_variable("module.path:variable_name", BaseTool)` 执行 `import_module` → `getattr` → `isinstance` 校验，将字符串路径转为运行时对象
- **依赖缺失友好提示**：导入失败时自动匹配已知 provider 包名，生成 `uv add langchain-google-genai` 这样的可操作提示
- **类型安全**：`expected_type` 参数确保解析结果一定是 `BaseTool` 实例

**Why：** 工具来源多样（沙箱、MCP、社区、ACP），硬编码会导致每次新增工具都要改核心逻辑。用 `"module:variable"` 声明式注册，新增工具只需配置一行。

**How to apply：** Agent 系统的工具注册应该是配置驱动的。用反射模式（`module:variable` 字符串 → 运行时 import + isinstance 校验）把"有哪些工具"和"工具怎么实现"彻底分开。

## 2. 能力感知的条件加载——工具随模型能力动态裁剪

`get_available_tools` 中有多处条件注入：

- `view_image_tool` 仅在 `model_config.supports_vision` 时加入
- `task_tool` 仅在 `subagent_enabled` 时加入
- `tool_search` 延迟注册表仅在 MCP 工具存在且 `tool_search.enabled` 时激活
- `is_host_bash_allowed` 检查确保 LocalSandbox 下不暴露宿主机 bash

工具列表不是固定的，而是根据运行时能力动态组装的。

**Why：** 不同模型能力差异大（有的支持视觉，有的不支持），硬塞不支持的工具有浪费 token、触发幻觉调用的风险。

**How to apply：** 工具加载必须做"能力门控"——每个工具标注前置条件（模型能力、运行时开关），加载时逐一检查，只注入当前上下文真正可用的工具。这和 `001-agent.md` 中的"功能降级"原则是一脉相承的。

## 3. Tool Search 延迟加载——用"按需发现"对抗上下文膨胀

当 MCP 工具很多时，把所有工具 schema 全部塞进 context 会浪费大量 token。`tool_search` 机制把 MCP 工具放入 `DeferredToolRegistry`，Agent 只看到工具名称列表（`<available-deferred-tools>`），需要时才通过 `tool_search` 查询获取完整 schema。

关键设计细节：

- **两阶段发现**：名称列表始终可见，完整 schema 按需获取
- **晋升机制**：查询后工具被 `promote()` 从延迟列表移除，后续调用不再被 `DeferredToolFilterMiddleware` 过滤
- **ContextVar 隔离**：用 `contextvars.ContextVar` 实现请求级注册表，并发请求互不干扰（asyncio 每个图运行在独立上下文，同步线程继承上下文副本）
- **三种查询模式**：`select:name1,name2`（精确选择）、`+keyword query`（名称必须包含关键词）、`keyword query`（正则搜索）

**Why：** MCP 生态中一个服务器可能暴露几十个工具，全量加载会让 system prompt 膨胀到不可用。延迟加载把"知道有什么"和"知道怎么用"分两步走。

**How to apply：** 当工具数量超过阈值（如 >20），不要把所有 schema 一次性加载到 context。用"名称列表 + 按需搜索获取完整定义"的两阶段策略。这是一个通用的 Agent 设计模式：**context 是稀缺资源，工具加载应该和内存分页一样做虚拟化**。

## 附：四源组装管线

```
config.yaml 工具  ──→  resolve_variable 反射加载  ──→  loaded_tools
内置工具          ──→  条件筛选（subagent/vision）  ──→  builtin_tools
MCP 工具          ──→  缓存 + 延迟注册表            ──→  mcp_tools / deferred
ACP 工具          ──→  外部代理协议                  ──→  acp_tools
                                                                  ↓
                                              loaded_tools + builtin_tools + mcp_tools + acp_tools
```
