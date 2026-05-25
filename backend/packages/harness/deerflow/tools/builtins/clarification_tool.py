"""澄清请求工具（Clarification Tool）

本模块实现了 `ask_clarification` 工具，允许代理在需要用户输入时中断执行
并向用户展示澄清问题。

工作机制：
--------
此工具的实现是一个**占位符**——实际的澄清逻辑由 `ClarificationMiddleware` 处理。
当代理调用此工具时，中间件会拦截工具调用并中断执行流程，将问题展示给用户。

关键特性：
--------
- **return_direct=True**：工具的返回值直接传回给代理，不经过进一步处理
- **五种澄清类型**：
  - missing_info：缺少必要信息（如文件路径、URL、具体需求）
  - ambiguous_requirement：需求存在多种合理解释
  - approach_choice：多种有效实现方案需要用户选择
  - risk_confirmation：危险操作需要明确确认（如删除文件、修改生产环境）
  - suggestion：有建议但需要用户批准

最佳实践：
--------
- 每次只问一个澄清问题
- 问题要具体、清晰
- 不要在需要澄清时做假设
- 对于危险操作，始终请求确认

拦截流程：
--------
1. 代理调用 ask_clarification 工具
2. ClarificationMiddleware 检测到工具调用
3. 中间件中断执行流程（抛出中断信号）
4. 问题被展示给用户
5. 用户回复后，执行流程恢复
"""

from typing import Literal

from langchain.tools import tool


@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user for clarification when you need more information to proceed.

    当需要用户提供更多信息才能继续执行时，使用此工具请求澄清。

    使用场景：

    - **缺少信息 (missing_info)**：未提供必要的详细信息（如文件路径、URL、具体需求）
    - **需求模糊 (ambiguous_requirement)**：存在多种合理的解释
    - **方案选择 (approach_choice)**：存在多种有效的实现方案，需要用户偏好
    - **危险操作 (risk_confirmation)**：需要明确确认的破坏性操作（如删除文件、修改生产环境）
    - **建议确认 (suggestion)**：有推荐方案但需要用户批准后才能继续

    执行将被中断，问题将展示给用户。等待用户回复后再继续。

    何时使用 ask_clarification：
    - 用户请求中缺少必要信息
    - 需求可以有多种解释
    - 存在多种有效的实现方案
    - 即将执行潜在危险的操作
    - 有建议但需要用户批准

    最佳实践：
    - 每次只问一个澄清问题以确保清晰
    - 问题要具体、明确
    - 需要澄清时不要做假设
    - 对于危险操作，始终请求确认
    - 调用此工具后，执行将自动中断

    Args:
        question: 向用户提出的澄清问题。要具体、明确。
        clarification_type: 澄清类型（missing_info、ambiguous_requirement、approach_choice、risk_confirmation、suggestion）。
        context: 可选的上下文信息，解释为什么需要澄清。帮助用户理解情况。
        options: 可选的选项列表（用于 approach_choice 或 suggestion 类型）。为用户提供明确的选择。
    """
    # 这是一个占位符实现
    # 实际逻辑由 ClarificationMiddleware 处理，它会拦截此工具调用
    # 并中断执行以将问题展示给用户
    return "Clarification request processed by middleware"
