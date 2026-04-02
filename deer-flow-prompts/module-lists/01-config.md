# 01-配置系统模块文件清单

## 模块概述

**路径**：`backend/packages/harness/deerflow/config/`

**核心作用**：定义整个系统的配置模型，支持环境变量、单例缓存、热重载

**设计理念**：Pydantic + 单例模式 + mtime 驱动的配置热更新

## 文件清单

### 1. __init__.py
- **路径**：`config/__init__.py`
- **核心导出**：
  - `get_app_config` - 获取应用配置单例
  - `Paths` - 路径配置类
  - `get_paths` - 获取路径配置
  - `SkillsConfig` - 技能配置类
  - `ExtensionsConfig` - 扩展配置类
  - `get_extensions_config` - 获取扩展配置
  - `MemoryConfig` - 记忆配置类
  - `get_memory_config` - 获取记忆配置
  - `get_tracing_config` - 获取追踪配置
  - `is_tracing_enabled` - 检查追踪是否启用
- **职责**：模块入口，导出核心配置函数

### 2. app_config.py
- **路径**：`config/app_config.py`
- **核心类**：
  - `AppConfig` - 应用主配置类
    - `resolve_config_path()` - 解析配置文件路径
    - `from_file()` - 从文件加载配置
    - `from_dict()` - 从字典加载配置
    - `get_app_config()` - 获取单例配置
- **职责**：应用主配置，包含所有子配置

### 3. model_config.py
- **路径**：`config/model_config.py`
- **核心类**：
  - `ModelConfig` - 模型配置类
    - `name` - 模型唯一名称
    - `use` - 模型提供者类路径
    - `model` - 模型名称
    - `supports_thinking` - 是否支持思考模式
    - `supports_vision` - 是否支持视觉
    - `when_thinking_enabled` - 思考模式额外配置
- **职责**：LLM 模型配置定义

### 4. agents_config.py
- **路径**：`config/agents_config.py`
- **职责**：代理配置（LeadAgent 配置）

### 5. subagents_config.py
- **路径**：`config/subagents_config.py`
- **职责**：子代理配置（并发控制、超时设置）

### 6. skills_config.py
- **路径**：`config/skills_config.py`
- **核心类**：
  - `SkillsConfig` - 技能配置类
    - `path` - 技能目录路径
    - `container_path` - 容器内路径
- **职责**：技能系统配置

### 7. memory_config.py
- **路径**：`config/memory_config.py`
- **核心类**：
  - `MemoryConfig` - 记忆配置类
    - `enabled` - 是否启用
    - `storage_path` - 存储路径
    - `debounce_seconds` - 防抖时间
    - `model_name` - 更新模型
    - `max_facts` - 最大事实数
    - `fact_confidence_threshold` - 事实置信度阈值
- **职责**：记忆系统配置

### 8. sandbox_config.py
- **路径**：`config/sandbox_config.py`
- **核心类**：
  - `SandboxConfig` - 沙箱配置类
    - `use` - 沙箱提供者类路径
- **职责**：沙箱系统配置

### 9. guardrails_config.py
- **路径**：`config/guardrails_config.py`
- **核心类**：
  - `GuardrailsConfig` - 安全护栏配置类
    - `enabled` - 是否启用
    - `provider` - 护栏提供者类路径
- **职责**：安全护栏配置

### 10. checkpointer_config.py
- **路径**：`config/checkpointer_config.py`
- **职责**：检查点配置（状态持久化）

### 11. stream_bridge_config.py
- **路径**：`config/stream_bridge_config.py`
- **职责**：流式桥接配置

### 12. tracing_config.py
- **路径**：`config/tracing_config.py`
- **职责**：追踪配置（分布式追踪）

### 13. token_usage_config.py
- **路径**：`config/token_usage_config.py`
- **职责**：Token 使用统计配置

### 14. summarization_config.py
- **路径**：`config/summarization_config.py`
- **职责**：摘要配置（上下文压缩）

### 15. title_config.py
- **路径**：`config/title_config.py`
- **职责**：标题生成配置

### 16. tool_config.py
- **路径**：`config/tool_config.py`
- **核心类**：
  - `ToolConfig` - 工具配置类
  - `ToolGroupConfig` - 工具组配置类
- **职责**：工具配置定义

### 17. tool_search_config.py
- **路径**：`config/tool_search_config.py`
- **职责**：工具搜索配置（延迟加载）

### 18. extensions_config.py
- **路径**：`config/extensions_config.py`
- **核心类**：
  - `ExtensionsConfig` - 扩展配置类
    - `mcp_servers` - MCP 服务器配置
    - `skills` - 技能状态配置
  - `get_extensions_config()` - 获取扩展配置单例
- **职责**：MCP 和技能扩展配置

### 19. acp_config.py
- **路径**：`config/acp_config.py`
- **职责**：ACP 代理配置

### 20. paths.py
- **路径**：`config/paths.py`
- **核心类**：
  - `Paths` - 路径配置类
    - `base_dir` - 基础目录
    - `threads_dir` - 线程目录
    - `skills_dir` - 技能目录
    - `memory_path` - 记忆文件路径
  - `get_paths()` - 获取路径配置单例
- **职责**：路径配置管理

## 核心设计模式

### 1. 单例模式
- `get_app_config()` 返回全局唯一配置实例
- mtime 变化时自动重新加载

### 2. 环境变量解析
- `$VAR` 语法自动解析环境变量
- 递归解析支持嵌套引用

### 3. 配置分层
- 主配置包含所有子配置
- 子配置可独立管理和测试

### 4. Pydantic 验证
- 自动类型转换
- 运行时验证
- JSON Schema 生成

## 关键依赖

- `pydantic` - 配置模型基础
- `yaml` - 配置文件解析
- `dotenv` - 环境变量加载

## 相关模块

- **被依赖**：所有模块都需要配置
- **依赖**：无（基础模块）
