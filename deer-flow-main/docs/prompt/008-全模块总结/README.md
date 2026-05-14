# 008 - 全模块总结

本目录是 `docs/prompt/` 下 001-007 七个模块的总结与串联，帮助快速理解整个 DeerFlow Agent 框架的架构全貌。

## 模块总览

| 编号 | 模块 | 核心职责 |
|------|------|----------|
| 001 | Agent Factory | 两层工厂：SDK 层组装 + 应用层业务策略 |
| 002 | Config System | YAML + Pydantic 类型安全配置，四级路径解析 |
| 003 | Model Factory | 反射加载模型 Provider，凭据链回退，Thinking 特性处理 |
| 004 | Tools System | 四层工具聚合（config/builtin/MCP/ACP），延迟加载，沙箱安全 |
| 005 | State Schema | TypedDict 继承 + Annotated reducers，中间件声明最小状态子集 |
| 006 | Middleware System | 每个中间件精确占据一个 hook 点，运行时分 lead/subagent 链 |
| 007 | Prompt Template | Python f-string 动态组装，条件注入，SOUL.md 独立管理 |

## 架构关系

```
Config (002) ──────────────────────────────────────────┐
                                                        │
Agent Factory (001) ───┬── Model Factory (003)          │
                       ├── Tools System (004)            │
                       ├── State Schema (005)            │
                       ├── Middleware System (006) ──────┘
                       └── Prompt Template (007)
```

Agent Factory 是中心枢纽，按需调用其余六个模块完成 Agent 组装。
Config System 是基础设施，被所有模块依赖。

## 详细总结

参见 [summary.md](./summary.md)。
