# 02 - 实现机制深度分析

> 本文档基于 `backend/packages/harness/deerflow/reflection/` 源码逐层拆解实现细节。回答"代码怎么写的、为什么这么写"。

---

## 一、模块结构

```
reflection/
└── __init__.py    # resolve_variable() + resolve_class()
```

仅一个文件，是系统中最小的模块，但被几乎所有模块依赖。

---

## 二、核心函数

### 2.1 resolve_variable(path)

```python
def resolve_variable(path: str):
    """加载模块属性。格式: 'module.path:variable_name'"""
    module_path, _, attr_name = path.rpartition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)
```

**调用链**:
```
config.yaml: use: "deerflow.community.tavily.tools:web_search_tool"
    ↓
resolve_variable("deerflow.community.tavily.tools:web_search_tool")
    ↓
importlib.import_module("deerflow.community.tavily.tools")
    ↓
getattr(module, "web_search_tool")
    ↓
返回 @tool 装饰后的函数对象
```

### 2.2 resolve_class(path, base_class)

```python
def resolve_class(path: str, base_class: type):
    """加载类并验证继承关系。"""
    cls = resolve_variable(path)
    if not issubclass(cls, base_class):
        raise TypeError(f"{cls} is not a subclass of {base_class}")
    return cls
```

**与 resolve_variable 的区别**: 增加了 `issubclass` 校验。用于 Provider、Storage 等需要满足接口契约的场景。

---

## 三、错误处理策略

```python
try:
    module = importlib.import_module(module_path)
except ImportError as e:
    # 生成可操作的安装提示
    hint = _generate_install_hint(e.name)
    raise ImportError(f"{e}. {hint}") from e
```

**安装提示映射**: 根据缺失的模块名推断 pip 包名（如 `langchain_google_genai` → `langchain-google-genai`）。
