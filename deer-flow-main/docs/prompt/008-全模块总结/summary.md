# DeerFlow 全模块架构总结

---

## 001 - Agent Factory（代理工厂）

### 核心设计
- **两层工厂**：SDK 层 `create_deerflow_agent` 负责纯参数组装，应用层 `make_lead_agent` 负责业务策略
- **动态组装**：模型、工具、中间件链、Prompt、状态 Schema 五大组件按需装配

### 关键机制
| 机制 | 说明 |
|------|------|
| 三级模型优先链 | 请求参数 → agent_config.model → 配置默认模型 |
| RuntimeFeatures 三态 | True（默认中间件）/ False（禁用）/ 实例（自定义） |
| @Next/@Prev 装饰器 | 中间件定位，声明式控制链中位置 |
| 工具去重 | 用户工具优先于配置工具 |

### 关键源码
- `factory.py` — 核心工厂逻辑
- `features.py` — RuntimeFeatures 三态标志 + 定位装饰器
- `agent.py` — Lead Agent 业务层工厂
- `thread_state.py` — 线程状态定义

---

## 002 - Config System（配置系统）

### 核心设计
- **YAML + Pydantic**：类型安全，自动环境变量解析
- **懒加载**：子系统配置（memory、agents、tools 等）按需初始化

### 关键机制
| 机制 | 说明 |
|------|------|
| 四级路径解析 | 构造参数 → 环境变量 → 当前目录 → 父目录 |
| 单例 + mtime 自动重载 | 配置变更自动检测 |
| 版本校验 | 与 config.example.yaml 对齐 |
| 路径安全 | regex + relative_to 双重验证 |

### 关键源码
- `app_config.py` — 主配置实现
- `model_config.py` — 模型配置 Schema
- `agents_config.py` — 自定义 Agent 配置加载
- `paths.py` — 目录布局与路径解析

---

## 003 - Model Factory（模型工厂）

### 核心设计
- **反射加载**：不硬编码 if-else，通过类名动态实例化 Provider
- **凭据链回退**：多来源凭据按优先级加载

### 关键机制
| 机制 | 说明 |
|------|------|
| 配置排除元数据 | 防止敏感字段泄漏到构造函数 |
| Thinking 特性处理 | 显式禁用注入、快捷方式深度合并 |
| 无效参数静默移除 | 如 reasoning_effort 不支持时自动丢弃 |
| Codex 特殊端点 | 映射到 Responses API |
| Provider Patch 继承 | 修复 SDK Bug（DeepSeek/MiniMax/OpenAI） |
| Tracing 附加 | 非阻塞失败处理 |

### 关键源码
- `factory.py` — 主工厂
- `resolvers.py` — 类反射工具
- `credential_loader.py` — 多源凭据加载
- `claude_provider.py` — Claude ChatModel（OAuth + 缓存）
- `openai_codex_provider.py` — Codex Responses API
- `patched_*.py` — 各 Provider 的 SDK 补丁

---

## 004 - Tools System（工具系统）

### 核心设计
- **四层聚合**：config tools → builtin tools → MCP tools → ACP tools
- **延迟加载**：ToolSearch 按 token 预算延迟加载 MCP 工具

### 关键机制
| 机制 | 说明 |
|------|------|
| Groups 过滤 | 仅对 config tools 生效，builtin/MCP/ACP 始终可用 |
| 条件注入 | subagent_enabled → task_tool，vision → view_image_tool |
| 沙箱安全 | bash 工具过滤 + 路径双重验证 + 输出脱敏 |
| ContextVar 隔离 | 每请求工具注册表独立 |
| ToolSearch | 延迟加载，仅列出名称不含 Schema |

### 关键源码
- `tools.py` — 主工具组装
- `tool_config.py` — 工具配置 Schema
- `tool_search.py` — 延迟工具注册与搜索
- `security.py` — 本地工具安全检查
- `task_tool.py` — 子代理委托（异步执行）
- `view_image_tool.py` — 图片查看（base64 编码）
- `present_file_tool.py` — 文件展示（虚拟路径规范化）

---

## 005 - State Schema（状态模式）

### 核心设计
- **TypedDict 继承**：从 AgentState 继承，不独立定义 messages
- **Annotated reducers**：并发写入安全合并

### 关键机制
| 机制 | 说明 |
|------|------|
| artifacts reducer | 去重合并 |
| viewed_images reducer | 清空语义（空列表时清除） |
| NotRequired | 可选字段避免初始化要求 |
| 最小状态子集 | 中间件只声明自己需要的字段 |
| viewed_images 清除 | 注入后清空防止重复 |
| 标题单次写入 | 中间件检查防止覆盖 |
| 懒目录初始化 | 避免不必要的 I/O |

### 关键源码
- `thread_state.py` — 状态定义与 reducers
- `thread_data_middleware.py` — 线程数据初始化
- `title_middleware.py` — 自动标题生成
- `clarification_middleware.py` — 工具澄清拦截
- `uploads_middleware.py` — 文件上传处理
- `view_image_middleware.py` — 图片注入与清除

---

## 006 - Middleware System（中间件系统）

### 核心设计
- **精确占位**：每个中间件只占据一个 hook 点（before/after/wrap）
- **运行时分离**：lead 和 subagent 有不同的中间件链

### 关键机制
| 中间件 | hook | 行为 |
|--------|------|------|
| LoopDetection | before_model | 警告阈值 3，硬限制 5 |
| ToolErrorHandling | after_tool | 异常转 ToolMessage，不中断循环 |
| DanglingToolCall | wrap_model_call | 修复悬挂工具调用 |
| DeferredToolFilter | before_model | 移除延迟工具 Schema，保留执行 |
| SubagentLimit | after_model | 截断非拒绝，钳位 [2,4] |
| MemoryFilter | before_model | 移除中间步骤和 uploaded_files |
| ClarificationMiddleware | 链末端 | 必须是链中最后一个 |

### 关键源码
- `loop_detection_middleware.py` — 循环检测
- `tool_error_handling_middleware.py` — 错误处理
- `dangling_tool_call_middleware.py` — 悬挂调用修复
- `deferred_tool_filter_middleware.py` — 延迟工具过滤
- `subagent_limit_middleware.py` — 并发限制
- `token_usage_middleware.py` — Token 用量记录
- `memory_middleware.py` — 记忆持久化
- `clarification_middleware.py` — 澄清拦截

---

## 007 - Prompt Template（Prompt 模板）

### 核心设计
- **Python f-string 模板**（非 Jinja2），条件注入
- **空字符串禁用**：空值不产生多余换行

### 关键机制
| 机制 | 说明 |
|------|------|
| SOUL.md 独立 | 与结构化配置分离，XML 包装 |
| 子代理段重复 | 并发限制重复 15+ 次（LLM 指令遵从） |
| 记忆上下文 | Token 限制 + XML 标签 |
| 技能渐进加载 | 先读主文件 |
| 延迟工具 | 仅列名称不含 Schema |
| 当前日期注入 | 防止过时信息 |
| 单一入口 | `apply_prompt_template` |

### 关键源码
- `prompt.py` — 完整 Prompt 模板系统

---

## 串联总结

### 数据流

```
用户请求
  → Config (002) 加载配置
  → Model Factory (003) 创建模型实例
  → Tools System (004) 聚合工具集
  → State Schema (005) 初始化线程状态
  → Middleware System (006) 组装中间件链
  → Prompt Template (007) 生成动态 Prompt
  → Agent Factory (001) 整合以上所有组件，产出可执行 Agent
```

### 设计原则

1. **声明式优于命令式**：RuntimeFeatures 三态、@Next/@Prev 定位
2. **懒加载与延迟求值**：Config 子系统、ToolSearch、目录初始化
3. **安全边界**：沙箱路径验证、凭据排除、输出脱敏
4. **可扩展性**：反射加载 Provider、四层工具聚合、中间件 hook 点
5. **防御性编程**：循环检测、错误转 ToolMessage、标题单次写入
