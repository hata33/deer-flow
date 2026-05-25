# 同步包装器

## 概述

DeerFlow 的工具系统同时支持异步和同步调用路径。异步工具（如 MCP 工具、ACP 代理工具）通过 `make_sync_tool_wrapper()` 自动包装为同步可调用对象，使嵌入式 DeerFlowClient 等同步调用者也能正常使用这些工具。

## 为什么需要同步桥接？

DeerFlow 有两种主要的工具调用路径：

1. **异步路径**：LangGraph 图执行 → 直接调用 `tool.coroutine`
2. **同步路径**：嵌入式 DeerFlowClient → 通过 `tool.func` 同步调用

许多工具（特别是 MCP 工具和 ACP 代理工具）的实现是纯异步的——只定义了 `coroutine` 而没有 `func`。同步调用路径需要一个同步的 `func` 才能工作。

`make_sync_tool_wrapper()` 解决了这个矛盾：它为异步工具自动生成同步包装器。

## make_sync_tool_wrapper

### 函数签名

```python
def make_sync_tool_wrapper(
    coro: Callable[..., Any],
    tool_name: str
) -> Callable[..., Any]
```

### 参数说明

- `coro`：异步可调用对象（工具的 `coroutine` 属性）
- `tool_name`：工具名称，用于错误日志

### 返回值

适合用作 `BaseTool.func` 的同步可调用对象。

## 事件循环检测策略

包装器内部的核心逻辑是根据事件循环的运行状态选择不同的执行策略：

```
尝试获取运行中的事件循环（asyncio.get_running_loop()）
    │
    ├── 成功（循环正在运行）
    │   └── 在 ThreadPoolExecutor 中执行 asyncio.run()
    │       - 复制当前 contextvars 上下文
    │       - 阻塞等待结果（future.result()）
    │
    └── 失败（循环未运行）
        └── 直接在当前线程中执行 asyncio.run()
```

### 为什么不能直接 asyncio.run()？

`asyncio.run()` 会创建一个新的事件循环。如果当前线程已经有一个运行中的事件循环，调用 `asyncio.run()` 会抛出 `RuntimeError`。因此需要检测事件循环状态并选择不同的执行策略。

### 为什么需要 ThreadPoolExecutor？

当事件循环已在运行时（如在 asyncio 任务的上下文中调用同步工具），我们不能在当前线程创建另一个事件循环。解决方案是在新的工作线程中创建独立的事件循环来运行异步协程。

### contextvars 传递

`make_sync_tool_wrapper` 使用 `contextvars.copy_context()` 将当前的上下文变量（如 `deferred_tool_registry`）复制到工作线程中：

```python
context = contextvars.copy_context()
future = _SYNC_TOOL_EXECUTOR.submit(
    context.run,
    lambda: asyncio.run(coro(*args, **kwargs))
)
```

这确保了延迟工具注册表等 ContextVar 在同步包装器中正确可用。

## RunnableConfig 传递

如果异步函数声明了 `RunnableConfig` 类型的参数，包装器会自动暴露 `config: RunnableConfig` 参数：

```python
# 检测 RunnableConfig 参数
config_param = _get_runnable_config_param(coro)

if config_param:
    # 带 RunnableConfig 的包装器
    def sync_wrapper(*args, config: RunnableConfig = None, **kwargs):
        if config is not None or config_param not in kwargs:
            kwargs[config_param] = config
        return run_coroutine(*args, **kwargs)
```

这覆盖了 DeerFlow 中需要配置感知的工具，如 `invoke_acp_agent`。

## 线程池配置

```python
_SYNC_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=10,
    thread_name_prefix="tool-sync"
)
```

- 最大 10 个工作线程
- 线程名前缀为 `tool-sync`（方便调试和日志追踪）
- 进程退出时通过 `atexit` 注册自动关闭（非阻塞）

## _ensure_sync_invocable_tool

工具装配管线中的 `_ensure_sync_invocable_tool()` 自动检测并处理需要同步包装的工具：

```python
def _ensure_sync_invocable_tool(tool: BaseTool) -> BaseTool:
    if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
        tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)
    return tool
```

在 `get_available_tools()` 中，所有工具（配置工具、内置工具、MCP 工具、ACP 工具）都会经过此函数处理。

## 使用示例

### 直接使用

```python
from deerflow.tools.sync import make_sync_tool_wrapper

# 为纯异步工具添加同步包装器
my_async_tool.func = make_sync_tool_wrapper(
    my_async_tool.coroutine,
    my_async_tool.name
)

# 现在可以通过 func 同步调用
result = my_async_tool.func(query="hello")
```

### 通过装配管线自动处理

```python
from deerflow.tools import get_available_tools

# 所有工具自动处理同步包装
tools = get_available_tools()
for tool in tools:
    assert tool.func is not None  # 所有工具都有同步入口
```

## 注意事项

1. **性能开销**：同步包装器需要线程切换和事件循环创建，比直接异步调用有额外开销
2. **线程安全**：ContextVar 通过 `copy_context()` 正确传递，但线程局部存储不会自动传递
3. **参数冲突**：包装器不合成动态函数签名。如果异步工具有一个名为 `config` 的用户参数和一个不同名称的 `RunnableConfig` 参数，可能产生冲突
4. **错误处理**：包装器捕获所有异常并通过 logger 记录后重新抛出
