# 反射系统 — 全局概览

## 定位

DeerFlow 反射模块（`deerflow.reflection`）提供动态模块加载和符号解析能力，允许系统在运行时通过字符串路径导入 Python 模块、获取变量和类，并进行类型验证。它是配置驱动架构的基础——模型工厂、工具加载、沙箱 Provider 等所有可插拔组件都依赖反射系统从 `config.yaml` 中的字符串路径实例化实际对象。

> **关键边界**：反射模块只负责"把字符串路径变成 Python 对象"，不负责"对象如何被使用"。后者由各业务模块（模型工厂、工具系统等）自行处理。

## 源文件

```
backend/packages/harness/deerflow/reflection/
└── resolvers.py    # resolve_variable / resolve_class / 安装提示映射
```

## 解决的核心问题

| 问题 | 反射模块的解决方案 |
|------|---------------------|
| **配置驱动的动态加载** | `resolve_variable()` 接受 `"module.path:variable_name"` 格式的字符串路径，自动拆分模块路径和变量名，通过 `importlib.import_module` 完成动态导入 |
| **缺失依赖的可操作错误** | `MODULE_TO_PACKAGE_HINTS` 映射已知 LangChain 提供商包的 pip/uv 安装名称，当导入失败时生成包含具体安装命令的错误信息 |
| **运行时类型安全** | `expected_type` 参数支持 `isinstance()` 检查，`resolve_class()` 额外验证 `issubclass()` 继承关系，确保配置中的类路径确实符合预期基类 |
| **统一错误处理** | 将 `ModuleNotFoundError`、`ImportError`、`AttributeError` 统一转换为带上下文信息的 `ImportError` 或 `ValueError`，方便上层定位问题 |

## 核心函数详解

### `resolve_variable(variable_path, expected_type=None)`

通过路径字符串动态解析一个 Python 变量。

**路径格式**：

```
module.path:variable_name
```

- 冒号左侧是完整的模块路径（Python import 路径）
- 冒号右侧是模块中的变量名、函数名或类名

**示例**：

```python
# 从 langchain_openai 模块导入 ChatOpenAI 类
cls = resolve_variable("langchain_openai:ChatOpenAI")

# 从自定义工具模块导入工具函数，同时验证类型
tool = resolve_variable("my_tools.web:search_tool", expected_type=BaseTool)
```

**类型验证**：

- `expected_type` 接受单个 `type` 或 `type` 元组
- 使用 `isinstance()` 检查解析结果
- 验证失败抛出 `ValueError`，包含实际类型和期望类型的信息

```python
# 元组类型验证
result = resolve_variable(path, expected_type=(ChatOpenAI, BaseChatModel))
```

### `resolve_class(class_path, base_class=None)`

通过路径字符串动态解析一个 Python 类，并可选地验证继承关系。

**内部流程**：

1. 调用 `resolve_variable(class_path, expected_type=type)` 确保解析结果是一个类
2. 检查 `isinstance(result, type)` 确保是类而非实例
3. 如果提供了 `base_class`，检查 `issubclass(result, base_class)`

**示例**：

```python
# 解析并验证基类
sandbox_cls = resolve_class(
    "deerflow.sandbox.local:LocalSandboxProvider",
    base_class=SandboxProvider,
)
```

## MODULE_TO_PACKAGE_HINTS 映射

当模块导入失败时，系统通过此映射将 Python 模块名转换为正确的 pip/uv 包名：

| 模块名（import 路径） | 包名（安装命令） |
|------------------------|------------------|
| `langchain_google_genai` | `langchain-google-genai` |
| `langchain_anthropic` | `langchain-anthropic` |
| `langchain_openai` | `langchain-openai` |
| `langchain_deepseek` | `langchain-deepseek` |

**错误信息示例**：

```
Could not import module langchain_google_genai.
Missing dependency 'langchain_google_genai'.
Install it with `uv add langchain-google-genai` (or `pip install langchain-google-genai`),
then restart DeerFlow.
```

对于不在映射表中的模块，`_build_missing_dependency_hint()` 会自动将下划线转换为连字符作为包名（如 `some_module` → `some-module`）。

## 错误处理策略

反射系统针对不同失败场景提供分层错误处理：

```
resolve_variable("path:var")
│
├─ 路径格式错误（无冒号）
│   └─ ImportError: "doesn't look like a variable path"
│
├─ 模块导入失败
│   ├─ ModuleNotFoundError
│   │   └─ ImportError + 安装提示（uv add / pip install）
│   └─ 其他 ImportError
│       └─ ImportError: "Error importing module ..."
│
├─ 属性不存在
│   └─ ImportError: "does not define a ... attribute/class"
│
└─ 类型验证失败
    └─ ValueError: "is not an instance of ..."
```

| 错误类型 | 触发条件 | 异常类型 | 用户可见信息 |
|----------|----------|----------|-------------|
| 路径格式无效 | 路径中无冒号分隔符 | `ImportError` | 包含正确格式示例 |
| 模块未安装 | `ModuleNotFoundError` | `ImportError` | 包含 `uv add` / `pip install` 安装命令 |
| 模块导入错误 | 其他 `ImportError` | `ImportError` | 保留原始错误信息 |
| 变量不存在 | `AttributeError` | `ImportError` | 明确指出模块中缺少哪个属性 |
| 类型不匹配 | `isinstance()` 失败 | `ValueError` | 显示期望类型和实际类型 |
| 非类对象 | `resolve_class` 中 `isinstance(x, type)` 失败 | `ValueError` | 指出路径不是有效类 |
| 继承关系不满足 | `issubclass()` 失败 | `ValueError` | 指出不是指定基类的子类 |

## 使用场景

### 1. 模型工厂

`deerflow.models.factory` 通过反射从配置实例化 LLM 模型：

```python
# config.yaml
models:
  - use: "langchain_openai:ChatOpenAI"
    model_name: gpt-4o

# factory.py 内部
model_cls = resolve_class(config.use, base_class=BaseChatModel)
model = model_cls(model_name=config.model_name, ...)
```

### 2. 工具加载

工具系统通过反射从配置加载自定义工具：

```python
# config.yaml
tools:
  - use: "my_package.tools:web_search"
    group: search

# 工具加载器内部
tool = resolve_variable(config.use, expected_type=BaseTool)
```

### 3. 沙箱 Provider

沙箱系统通过反射加载配置的 Provider 实现：

```python
# config.yaml
sandbox:
  use: "deerflow.sandbox.local:LocalSandboxProvider"

# 沙箱初始化内部
provider_cls = resolve_class(config.use, base_class=SandboxProvider)
provider = provider_cls()
```

## 生命周期

```
配置声明（config.yaml 中的 use 路径）
    │
    ▼
resolve_variable() / resolve_class() 被调用
    │
    ▼
路径拆分：module_path : variable_name
    │
    ▼
importlib.import_module(module_path)
    │
    ├─ 成功 → 继续下一步
    └─ 失败 → ImportError + 安装提示
    │
    ▼
getattr(module, variable_name)
    │
    ├─ 成功 → 继续下一步
    └─ 失败 → ImportError（属性不存在）
    │
    ▼
类型检查（isinstance / issubclass）
    │
    ├─ 通过 → 返回解析结果
    └─ 失败 → ValueError（类型不匹配）
```

## 设计决策

- **冒号分隔符**：使用 `:` 分隔模块路径和变量名，与 Python 的 `__import__` 和 `importlib` 生态保持一致的惯例（如 `django` 的 `import_string`、`werkzeug` 的 `import_string`）
- **延迟导入**：所有 `importlib.import_module` 调用发生在运行时而非模块加载时，确保未使用的可选依赖不会导致启动失败
- **错误链保留**：所有异常都通过 `from err` 保留原始异常链，方便调试时追踪根因
- **泛型支持**：函数签名使用 Python 3.12 的 `[T]` 泛型语法，让类型检查器能推断返回类型
