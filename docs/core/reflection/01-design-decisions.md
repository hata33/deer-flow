# 01 - 设计决策与考量

> 本文档回答"为什么这么做"——每个设计决策的动机、解决的问题、权衡取舍。

---

## 一、核心设计决策清单

| # | 决策 | 一句话动机 |
|---|------|-----------|
| 1 | **`module.path:variable` 字符串约定** | 统一的模块引用格式，消除歧义 |
| 2 | **importlib 动态加载而非硬编码 if/else** | 新增类型零代码修改 |
| 3 | **resolve_variable 与 resolve_class 分离** | 变量引用和类引用有不同的校验需求 |
| 4 | **缺失模块时返回可操作的安装提示** | 降低调试成本，避免晦涩的 ImportError |

---

## 二、逐决策分析

### 决策 1：`module.path:variable` 字符串约定

**问题**: config.yaml 中需要引用 Python 类和变量（如 LLM Provider、工具函数）。如何用字符串唯一标识一个 Python 对象？

| 方案 | 优势 | 劣势 |
|------|------|------|
| 完全限定类名 `a.b.C` | 标准 | 无法区分同模块的函数和类；与 Python import 语义不完全对齐 |
| `module.path:variable`（当前） | 明确分隔模块和属性；可引用任意对象 | 非标准格式，需要解析 |
| 自定义 DSL | 最灵活 | 复杂；学习成本 |

**选择冒号分隔格式**: `deerflow.models.vllm_provider:VllmChatModel` — 冒号前是 `importlib.import_module()` 的参数，冒号后是 `getattr()` 的参数。一行代码拆分即可解析，无需正则。

---

### 决策 2：importlib 动态加载

**问题**: 系统支持多种 LLM Provider、工具、沙箱实现，但不能 import 所有可能的依赖。

| 方案 | 优势 | 劣势 |
|------|------|------|
| 硬编码 if name == "x" | 编译时检查 | 新增类型必须改代码和重新部署 |
| entry_points 插件发现 | Python 标准机制 | 需要 pip install；开发时需重新安装 |
| importlib（当前） | 零耦合；配置即代码 | 拼写错误运行时才发现 |

**选择 importlib**: 牺牲编译时检查，换来"新增类型只需修改 config.yaml"的灵活性。拼写错误在启动时通过 ImportError 立即暴露。

---

### 决策 3：resolve_variable 与 resolve_class 分离

**问题**: 有时需要引用函数/变量（如工具函数），有时需要引用类（如 Provider）。两者有不同的校验需求。

**resolve_variable**: 加载任意属性，无类型校验。适用于工具函数、客户端实例。

**resolve_class**: 加载类并通过 `base_class` 参数验证继承关系。确保配置中引用的类确实符合接口契约。

这种分离避免了对函数做不必要的 isinstance 检查，同时保留了类引用的类型安全。

---

### 决策 4：可操作的安装提示

**问题**: 用户配置了一个尚未安装的 Provider（如 `langchain-google-genai`），默认的 `ImportError: No module named 'xxx'` 不够友好。

**选择**: 捕获 ImportError，根据模块名生成安装提示。例如：

```
ModuleNotFoundError: No module named 'langchain_google_genai'
→ Hint: Install it with: uv add langchain-google-genai
```

用户无需搜索文档即可知道如何解决。
