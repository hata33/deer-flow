# Agent 构建启示

> 来源：`backend/packages/harness/deerflow/agents/lead_agent/agent.py`

## 1. 中间件链是 Agent 的脊梁，顺序即语义

`_build_middlewares` 中 14 个中间件按严格顺序排列，每一层的放置都有因果关系。关键设计原则：

- **基础设施先行**：ThreadData → Uploads → Sandbox，先准备好环境
- **防御性处理靠前**：DanglingToolCall、ToolErrorHandling 修补异常，防止下游崩溃
- **功能增强居中**：Summarization、Todo、Title、Memory、Vision 按需插入
- **拦截器始终在最后**：ClarificationMiddleware 必须是链尾，确保所有处理完成后再中断

不要把 Agent 逻辑全部塞进 prompt 或一个大函数里。用中间件模式做**正交分解**，每个中间件只关心一件事，通过有序组合实现复杂行为。这样某个能力（如视觉、记忆）可以独立开关，不影响其他功能。

## 2. 用"功能降级"而非"硬失败"处理模型能力差异

`_resolve_model_name` 和 `make_lead_agent` 中的模型验证体现了这个思路：

- 模型名称找不到 → 降级到默认模型并 warning，不抛异常
- 请求了 thinking 但模型不支持 → 自动关闭 thinking，不拒绝请求
- 工具按模型能力动态裁剪（Vision 中间件只在 `supports_vision` 时加入）

生产级 Agent 必然面对多模型、多能力的现实。设计时要区分"必须满足的前置条件"（如至少有一个模型可用）和"可以优雅降级的增强功能"（如 thinking、vision）。让 Agent 在任何合理配置下都能工作，而不是一遇到不匹配就崩溃。

## 3. 工厂函数与配置驱动解耦——同一个创建入口，无数种运行时形态

`make_lead_agent` 通过一个 `RunnableConfig` 参数驱动所有行为差异（9 个可配置参数），而不需要为每种组合写不同的创建逻辑。同一份代码同时支持：

- **引导模式** vs **标准模式**（`is_bootstrap` 切换）
- **自定义智能体**（通过 `agent_name` 加载不同配置、工具组、人格）
- **计划模式**、**子智能体**、**思考模式** 按需组合

Agent 不应该是硬编码的单体。用一个工厂函数接受声明式配置，内部做解析、验证、组装，让调用方通过参数组合而非代码修改来获得不同行为。这样前端、CLI、API 都能用同一个入口创建出形态各异的 Agent。
