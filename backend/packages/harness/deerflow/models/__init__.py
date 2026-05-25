"""模型模块 — DeerFlow 的 LLM 模型抽象层。

模块功能
========
本模块是 DeerFlow 项目的 LLM（大语言模型）接入层，负责：
- 统一管理各类大语言模型的创建、配置和实例化
- 提供模型工厂函数 `create_chat_model`，支持通过配置文件动态选择模型
- 封装多种模型提供商的适配器（Claude、DeepSeek、MiniMax、vLLM、MindIE、OpenAI Codex 等）

核心设计
========
1. **工厂模式**: 通过 `create_chat_model()` 工厂函数，根据 YAML 配置文件动态创建
   不同的模型实例，无需在业务代码中硬编码模型类型。
2. **可插拔架构**: 每个模型提供商作为独立模块实现，通过 `use` 字段指定类路径，
   支持运行时动态加载（基于反射机制 `resolve_class`）。
3. **思维模式支持**: 统一抽象了模型的思考/推理能力（thinking/reasoning），
   包括启用/禁用切换、推理预算自动分配等。
4. **凭证自动加载**: 支持从 Claude Code CLI、Codex CLI 等工具自动加载 OAuth 凭证，
   实现无 API Key 的开发体验。

子模块概览
==========
- `factory`: 模型工厂，核心入口函数 `create_chat_model()`
- `claude_provider`: Anthropic Claude 提供商，支持 OAuth Bearer 认证和提示缓存
- `credential_loader`: 自动从 Claude Code CLI / Codex CLI 加载凭证
- `mindie_provider`: 华为昇腾 MindIE 推理引擎适配器
- `openai_codex_provider`: OpenAI Codex Responses API 适配器
- `patched_deepseek`: DeepSeek 模型补丁，修复推理内容丢失问题
- `patched_minimax`: MiniMax 模型补丁，修复推理输出字段丢失问题
- `patched_openai`: OpenAI 补丁，修复 Gemini 思维签名丢失问题
- `vllm_provider`: vLLM 推理引擎适配器，保留推理字段

使用场景
========
- 在 `config.yaml` 中声明模型配置，通过 `create_chat_model("model-name")` 创建实例
- Agent 节点通过本模块获取可用的 LLM 实例执行推理任务
- 支持在运行时动态切换不同模型提供商，无需修改业务代码
"""

# 导出核心工厂函数，作为模块的公共 API 入口
from .factory import create_chat_model

# 模块公开接口声明：仅暴露 create_chat_model 函数
__all__ = ["create_chat_model"]
