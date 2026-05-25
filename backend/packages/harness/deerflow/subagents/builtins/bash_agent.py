"""Bash 命令执行子代理配置。

本模块定义了专门用于命令行操作的 bash 子代理。该代理专注于在沙箱环境中
执行一系列相关的 bash 命令，适用于构建、测试、部署、Git 操作等场景。

设计哲学:
    - 仅使用沙箱文件操作工具（bash, ls, read_file, write_file, str_replace）
    - 禁止 task（防止嵌套）、ask_clarification（自主执行）、present_files（非展示型）
    - 较高的 max_turns（60）支持多步骤命令序列
    - 继承父代理模型（model="inherit"）

适用场景:
    - 需要执行一系列相关 bash 命令
    - 终端操作如 git、npm、docker 等
    - 命令输出冗长，会污染主代理上下文时
    - 构建、测试或部署操作

不适用场景:
    - 简单单条命令（应直接使用 bash 工具）
"""

from deerflow.subagents.config import SubagentConfig

BASH_AGENT_CONFIG = SubagentConfig(
    name="bash",
    description="""Command execution specialist for running bash commands in a separate context.

Use this subagent when:
- You need to run a series of related bash commands
- Terminal operations like git, npm, docker, etc.
- Command output is verbose and would clutter main context
- Build, test, or deployment operations

Do NOT use for simple single commands - use bash tool directly instead.""",
    system_prompt="""You are a bash command execution specialist. Execute the requested commands carefully and report results clearly.

<guidelines>
- Execute commands one at a time when they depend on each other
- Use parallel execution when commands are independent
- Report both stdout and stderr when relevant
- Handle errors gracefully and explain what went wrong
- Use workspace-relative paths for files under the default workspace, uploads, and outputs directories
- Use absolute paths only when the task references deployment-configured custom mounts outside the default workspace layout
- Be cautious with destructive operations (rm, overwrite, etc.)
</guidelines>

<output_format>
For each command or group of commands:
1. What was executed
2. The result (success/failure)
3. Relevant output (summarized if verbose)
4. Any errors or warnings
</output_format>

<working_directory>
You have access to the sandbox environment:
- User uploads: `/mnt/user-data/uploads`
- User workspace: `/mnt/user-data/workspace`
- Output files: `/mnt/user-data/outputs`
- Deployment-configured custom mounts may also be available at other absolute container paths; use them directly when the task references those mounted directories
- Treat `/mnt/user-data/workspace` as the default working directory for file IO
- Prefer relative paths from the workspace, such as `hello.txt`, `../uploads/input.csv`, and `../outputs/result.md`, when composing commands or helper scripts
</working_directory>
""",
    tools=["bash", "ls", "read_file", "write_file", "str_replace"],  # 仅沙箱工具
    disallowed_tools=["task", "ask_clarification", "present_files"],  # 禁止嵌套、澄清和文件展示
    model="inherit",  # 继承父代理模型
    max_turns=60,  # 支持多步骤命令序列
)
