# 状态与工厂启示

> 来源：`backend/packages/harness/deerflow/agents/thread_state.py`、`agents/factory.py`、`agents/features.py`

## 1. 状态字段按生命周期分类，用 Annotated reducer 封装合并策略

`ThreadState` 的 7 个字段按生命周期分为三类：**环境上下文**（sandbox、thread_data、uploaded_files，中间件初始化一次后只读）、**累积产物**（artifacts、viewed_images，多轮追加需去重/合并）、**覆盖型**（title、todos，最新值覆盖）。后两类用 `Annotated[list[str], merge_artifacts]` 和 `Annotated[dict, merge_viewed_images]` 声明自定义 reducer。`merge_artifacts` 通过 `dict.fromkeys` 去重保序；`merge_viewed_images` 用空字典 `{}` 作为清空信号（`len(new) == 0 → return {}`），中间件无需额外参数即可表达"全部清空"。

不要让每个中间件手动读旧值、合并、写回。用 LangGraph 的 Annotated reducer 把"怎么合并"编码在类型定义里，中间件只需返回增量部分。新增状态字段时定义一个纯函数 reducer，用 `Annotated` 挂上去，零侵入中间件代码。合并策略（去重、保序、清空）集中在状态定义文件里，调用方无感知。

## 2. 三态特性标志——True/False/自定义实例，一个参数控制"要不要"和"用什么"

`RuntimeFeatures` 的每个特性字段接受三种值：`True`（内置默认中间件）、`False`（禁用）、`AgentMiddleware` 实例（自定义替换）。`_assemble_from_features` 用 `isinstance` 分支统一处理。`summarization` 和 `guardrail` 没有内置默认值（需要模型参数等外部依赖），传 `True` 时显式报错而非静默跳过。

开关和替换不要拆成两个参数。布尔开关 + override 参数会导致参数翻倍，且调用方要自己处理互斥。三态设计把"要不要"和"用什么"合并为一个参数，API 表面积最小。对没有合理默认值的特性，`True` 必须报错——静默跳过会让用户误以为功能已启用。

## 3. @Next/@Prev 声明式定位——用"邻居"代替"索引"插入有序链

`create_deerflow_agent` 的 `extra_middleware` 参数允许外部代码插入自定义中间件。位置通过 `@Next(AnchorClass)` / `@Prev(AnchorClass)` 装饰器声明——`@Next(MemoryMiddleware)` 表示"放在 MemoryMiddleware 之后"。`_insert_extra` 算法处理冲突检测、无锚点默认插在 ClarificationMiddleware 之前、跨锚定迭代解析、循环依赖检测。最后强制 ClarificationMiddleware 回到链尾，防止 `@Next` 意外把它推离末位。

不要让调用方依赖绝对索引来定位。框架内部链顺序变化时，索引会静默错位。声明式锚点让调用方只说"我要在谁旁边"，框架保证语义正确。这是有序链扩展的通用模式：**扩展点从"知道位置"降级为"知道邻居"**，降低框架内部重构对外部代码的影响。
