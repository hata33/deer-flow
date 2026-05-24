"""跨频道共享命令定义。

**设计目的**

将所有 IM 频道支持的斜杠命令统一定义在一个地方，确保：

1. 各频道的命令解析器（如飞书的 ``_is_feishu_command``）和
   ChannelManager 的命令分发器始终保持同步
2. 新增或删除命令只需在此处修改一处即可生效

**当前支持的命令**

============  ==========================================
命令          功能
============  ==========================================
/bootstrap    启动引导会话（启用 Agent 初始化设置）
/new          开始新的对话
/status       显示当前线程信息
/models       列出可用模型
/memory       显示记忆状态
/help         显示帮助信息
============  ==========================================

**命令处理流程**

::

    用户在 IM 中输入 /xxx
        │
        ▼
    频道解析器判断是否为已知命令
        │ (是)
        ▼
    创建 InboundMessage(msg_type=COMMAND)
        │
        ▼
    ChannelManager._handle_command()
        │
        ├─ /bootstrap → 转为 CHAT 消息 + is_bootstrap 上下文
        ├─ /new       → 调用 Gateway API 创建新线程
        ├─ /status    → 查询当前线程 ID
        ├─ /models    → 通过 Gateway 查询模型列表
        ├─ /memory    → 通过 Gateway 查询记忆状态
        └─ /help      → 返回帮助文本
"""

from __future__ import annotations

KNOWN_CHANNEL_COMMANDS: frozenset[str] = frozenset(
    {
        "/bootstrap",
        "/new",
        "/status",
        "/models",
        "/memory",
        "/help",
    }
)
