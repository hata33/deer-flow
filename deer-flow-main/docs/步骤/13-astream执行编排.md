deer-flow-main\backend\packages\harness\deerflow\runtime\runs\worker.py:128

`agent.astream()` 是整个执行流程的入口，但 `run_agent` 本身不包含业务逻辑，它是一个**执行编排器**

agent 是层层组装出来的，完整的调用链路：

1. **`agent_factory`** → 即 `make_lead_agent(config)`，定义图的节点、边、工具
2. **`start_run`（004）** → 准备 `graph_input`、`config`、`stream_modes` 等运行参数
3. **`run_agent`（005 第 3 步）** → 注入 Runtime、挂载 checkpointer/store、设置中断节点
4. **`agent.astream()`** → LangGraph 框架接管，按图定义逐步执行节点，产出流式 chunk

`run_agent` 只负责取消检测、序列化、推流这些外围工作，核心的图执行逻辑由 LangGraph 框架提供，图的结构定义由 `make_lead_agent` 工厂函数决定

> 本步骤：`run_agent` 是执行编排层——组装 agent + 参数后交给 `agent.astream()` 运行，自己只管流式转发和生命周期管理，核心图执行逻辑在 LangGraph 框架和 agent 工厂中
