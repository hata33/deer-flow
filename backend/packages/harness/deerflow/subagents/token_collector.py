"""子代理 LLM Token 用量收集器。

本模块实现了一个轻量级的 LangChain 回调处理器，用于在子代理执行期间
收集每次 LLM 调用的 token 用量。每次子代理执行创建自己的收集器实例，
执行完成后通过 snapshot_records() 获取累积的用量记录，再由父代理的
RunJournal 通过 record_external_llm_usage_records() 合并到主日志中。

Token 收集流程:
    1. SubagentExecutor._aexecute() 创建 SubagentTokenCollector 实例
    2. 收集器作为 callback 传入 RunnableConfig
    3. 每次 LLM 调用结束后，on_llm_end() 从 response 中提取 usage_metadata
    4. 通过 run_id 去重，确保每个 LLM 调用仅记录一次
    5. 执行完成后 snapshot_records() 返回全部用量记录
    6. 记录存入 SubagentResult.token_usage_records
    7. 最终通过 RunJournal.record_external_llm_usage_records() 合并到父代理

用量记录格式:
    每条记录包含以下字段:
    - source_run_id: LangChain 运行 ID（用于去重）
    - caller: 调用方标识（格式: "subagent:{agent_name}"）
    - input_tokens: 输入 token 数
    - output_tokens: 输出 token 数
    - total_tokens: 总 token 数
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


class SubagentTokenCollector(BaseCallbackHandler):
    """轻量级回调处理器，收集子代理内 LLM 调用的 token 用量。

    每个 LLM 调用通过 run_id 去重，确保即使 LangChain 内部触发
    重复回调也不会重复计数。仅提取第一个含有有效 usage_metadata
    的 generation 的用量信息。

    用法:
        collector = SubagentTokenCollector(caller="subagent:bash")
        config = RunnableConfig(callbacks=[collector])
        # ... 执行代理 ...
        records = collector.snapshot_records()

    Attributes:
        caller: 调用方标识字符串，用于在用量记录中区分来源。
    """

    def __init__(self, caller: str):
        """初始化 token 收集器。

        Args:
            caller: 调用方标识，通常为 "subagent:{agent_name}" 格式。
        """
        super().__init__()
        self.caller = caller
        # 累积的用量记录列表
        self._records: list[dict[str, int | str]] = []
        # 已计数的 run_id 集合，用于去重
        self._counted_run_ids: set[str] = set()

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """LLM 调用结束时的回调处理。

        从 response.generations 中提取 usage_metadata，包含 input_tokens、
        output_tokens 和 total_tokens。每个 run_id 仅记录第一次遇到的
        有效用量信息，后续重复回调被忽略。

        Args:
            response: LLM 响应对象，包含 generations 列表。
            run_id: 本次 LLM 调用的唯一运行 ID。
            tags: 可选的标签列表。
            **kwargs: 其他回调参数。
        """
        rid = str(run_id)
        # 去重检查：已记录的 run_id 跳过
        if rid in self._counted_run_ids:
            return

        for generation in response.generations:
            for gen in generation:
                if not hasattr(gen, "message"):
                    continue
                # 从 AIMessage 的 usage_metadata 属性提取用量信息
                usage = getattr(gen.message, "usage_metadata", None)
                usage_dict = dict(usage) if usage else {}
                input_tk = usage_dict.get("input_tokens", 0) or 0
                output_tk = usage_dict.get("output_tokens", 0) or 0
                total_tk = usage_dict.get("total_tokens", 0) or 0
                # 如果 total_tokens 未提供，使用 input + output 的总和
                if total_tk <= 0:
                    total_tk = input_tk + output_tk
                # 跳过无效记录（total_tokens 为 0）
                if total_tk <= 0:
                    continue
                self._counted_run_ids.add(rid)
                self._records.append(
                    {
                        "source_run_id": rid,
                        "caller": self.caller,
                        "input_tokens": input_tk,
                        "output_tokens": output_tk,
                        "total_tokens": total_tk,
                    }
                )
                return

    def snapshot_records(self) -> list[dict[str, int | str]]:
        """返回累积用量记录的副本。

        返回的是浅拷贝列表，调用方可以安全修改而不影响内部状态。
        通常在子代理执行完成后调用一次，获取全部用量记录。

        Returns:
            用量记录列表，每条记录包含 source_run_id、caller、
            input_tokens、output_tokens、total_tokens。
        """
        return list(self._records)
