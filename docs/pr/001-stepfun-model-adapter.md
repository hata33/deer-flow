# 001 — feat(models): add StepFun reasoning model adapter

| 字段 | 值 |
|------|-----|
| PR | #3461 |
| Commit | `37337b77` |
| 状态 | Merged |
| 合并者 | WillemJiang |
| 合并时间 | 2026-06-09 |
| 分支 | → upstream/main |
| 改动规模 | 4 文件 +507 行 |

## 问题

DeerFlow 需要接入阶跃星辰（StepFun）的推理模型（step-3.7-flash、step-3.5-flash）。
这些模型的 reasoning 输出行为与标准 OpenAI 兼容接口不同：
- 推理内容通过非标准字段返回（`reasoning` 或 `reasoning_content`）
- 流式和非流式响应的 reasoning 捕获方式不同
- 多轮 tool-call 对话中，历史 assistant 消息需要回放 reasoning 内容

直接使用 `langchain_openai.ChatOpenAI` 无法正确捕获和处理这些 reasoning 字段。

## 根因

LangChain 的 `ChatOpenAI` 只处理标准的 OpenAI 响应格式。StepFun 的推理模型
在响应中添加了额外的 `reasoning` / `reasoning_content` 字段，这些字段：
1. 在流式响应中作为独立 chunk 类型出现
2. 在非流式响应中嵌套在 message 对象内
3. 需要在后续 tool-call 轮次中作为历史上下文传回

DeerFlow 已有类似适配器模式（如 `VllmChatModel`），但没有 StepFun 的适配。

## 方案

新增 `PatchedChatStepFun` 适配器（`deerflow/models/patched_stepfun.py`）：

1. **继承 `ChatOpenAI`**：保持 OpenAI 兼容协议的基础通信
2. **流式 reasoning 捕获**：在 `_stream` 方法中拦截 reasoning chunk，
   累积到 `additional_kwargs.reasoning` 中
3. **非流式 reasoning 捕获**：在 `_generate` 方法中从响应 message 的
   `reasoning` / `reasoning_content` 字段提取
4. **历史回放**：在 `bind_tools` 后的调用中，将累积的 reasoning 注入
   历史 assistant 消息的 `additional_kwargs`
5. **配置示例**：在 `config.example.yaml` 中添加 StepFun 模型配置模板

同时支持 `reasoning` 和 `reasoning_content` 两种字段名，兼容 StepFun
不同版本 API 的行为差异。

## 取舍

| 选择 | 理由 |
|------|------|
| 继承 `ChatOpenAI` 而非从零实现 | 复用 OpenAI 兼容协议的通信、认证、重试逻辑，减少维护负担 |
| `Patched` 前缀命名 | 遵循项目现有命名模式（`PatchedChatStepFun`），表明这是对基类的补丁式增强 |
| 适配器放在 `models/` 包内 | 与 `VllmChatModel` 等其他模型适配器保持一致的组织结构 |
| 17 个单元测试 | 覆盖流式/非流式 × 有/无 reasoning × 有/无 tool-call 的组合场景 |

**放弃的方案**：
- 修改 `ChatOpenAI` 上游代码 — 不现实，那是 LangChain 的代码
- 使用 callback 捕获 reasoning — callback 无法修改 message 结构，
  无法实现历史回放

## 验证

- 17 个单元测试覆盖所有响应路径
- `ruff check` + `ruff format --check` 通过
- `config.example.yaml` 配置模板已验证
