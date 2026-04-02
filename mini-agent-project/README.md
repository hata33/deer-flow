# Mini Agent

基于 DeerFlow 架构设计的简化版 AI 代理系统。

## 项目概述

Mini Agent 是一个功能精简但架构完整的 AI 代理框架，适合学习和快速开发。

### 核心特性

- **配置系统**: Pydantic + YAML + 环境变量 + 热更新
- **沙箱系统**: 本地文件系统隔离执行
- **工具系统**: 可扩展的工具注册机制
- **中间件**: 请求/响应处理管道
- **状态管理**: 对话历史和上下文管理

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 创建配置文件

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml` 设置你的 API 密钥：

```yaml
models:
  - name: gpt-4
    provider: openai
    model: gpt-4-turbo-preview
    api_key: ${OPENAI_API_KEY}
```

### 3. 设置环境变量

创建 `.env` 文件：

```
OPENAI_API_KEY=your_key_here
```

### 4. 运行

```bash
python main.py
```

## 项目结构

```
mini-agent/
├── config/              # 配置系统
│   ├── __init__.py
│   ├── app_config.py    # 主配置
│   ├── model_config.py  # 模型配置
│   └── paths.py         # 路径配置
├── agents/              # 代理系统
│   ├── __init__.py
│   ├── agent.py         # 主代理
│   ├── state.py         # 状态定义
│   └── middlewares.py   # 中间件
├── tools/               # 工具系统
│   ├── __init__.py
│   ├── registry.py      # 工具注册
│   └── builtins.py      # 内置工具
├── sandbox/             # 沙箱系统
│   ├── __init__.py
│   ├── base.py          # 抽象接口
│   └── local.py         # 本地实现
├── models/              # 模型工厂
│   ├── __init__.py
│   └── factory.py       # 模型创建
├── utils/               # 工具函数
│   ├── __init__.py
│   └── helpers.py
├── tests/               # 测试
│   ├── test_config.py
│   └── test_tools.py
├── config.yaml          # 配置文件
├── main.py              # 入口文件
└── requirements.txt
```

## 使用示例

### 基本对话

```python
from agents import create_agent

async def main():
    agent = await create_agent()
    response, state = await agent.chat("你好!")
    print(response)
```

### 自定义中间件

```python
from agents.middlewares import Middleware

class MyMiddleware(Middleware):
    async def before_request(self, state, input_text):
        # 请求前处理
        return input_text.upper()

agent = MiniAgent(middlewares=[MyMiddleware()])
```

### 自定义工具

```python
from tools import tool

@tool(name="my_tool", description="我的自定义工具")
def my_function(arg1: str) -> str:
    return f"处理结果: {arg1}"
```

## 与 DeerFlow 的对比

| 功能 | DeerFlow | Mini Agent |
|------|----------|------------|
| 配置文件 | 18个 | 3个核心配置 |
| 中间件 | 12个 | 5个基础中间件 |
| 工具 | 内置+MCP+社区 | 内置+自定义 |
| 沙箱 | Local + Docker | Local |
| 记忆 | LLM驱动 | 简单存储 |
| 检查点 | 支持 | 可选 |
| 子代理 | 支持 | 暂不支持 |

## 开发路线

- [x] 配置系统
- [x] 模型工厂
- [x] 沙箱系统
- [x] 工具系统
- [x] 代理引擎
- [ ] 记忆系统
- [ ] 检查点
- [ ] 子代理
- [ ] MCP 集成

## 运行测试

```bash
pytest tests/ -v
```

## License

MIT
