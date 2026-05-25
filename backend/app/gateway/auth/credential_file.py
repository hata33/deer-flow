"""安全凭据文件写入 — 替代日志输出敏感信息。

将明文密码写入 stdout/stderr 是众所周知的 CodeQL 安全问题
（py/clear-text-logging-sensitive-data）— 在生产环境中，日志
会被收集到 ELK/Splunk 等系统，成为敏感信息泄露源。

本模块的替代方案：
  - 将凭据写入 0600 权限文件，仅进程用户可读
  - 返回文件路径供调用者记录（记录路径而非密码）
  - 操作员可从文件中获取初始密码，使用后删除文件

使用场景：
  - 管理员首次创建时写入初始密码
  - 管理员密码重置时写入新密码

关键特性：
  - 使用 os.open + O_WRONLY|O_CREAT|O_TRUNC 实现原子 0600 创建
  - O_TRUNC（而非 O_EXCL）允许覆盖已有文件，无需 unlink + create 两步操作
  - 文件头部标注用途（initial/reset），帮助操作员区分事件类型
"""

from __future__ import annotations

import os
from pathlib import Path

from deerflow.config.paths import get_paths

# 凭据文件名
_CREDENTIAL_FILENAME = "admin_initial_credentials.txt"


def write_initial_credentials(email: str, password: str, *, label: str = "initial") -> Path:
    """将管理员邮箱和密码写入 {base_dir}/admin_initial_credentials.txt。

    文件以 0600 权限原子创建（通过 os.open），密码永远不会全局可读，
    即使在 write_text 和 chmod 之间的系统调用窗口期间也不会。

    label 参数区分"首次创建"（initial）和"密码重置"（reset），
    帮助重启后查看文件的操作员了解产生此文件的事件。

    Args:
        email: 管理员邮箱地址。
        password: 明文密码。
        label: 凭据来源标签（"initial" 或 "reset"）。

    Returns:
        凭据文件的绝对路径。
    """
    target = get_paths().base_dir / _CREDENTIAL_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)

    content = (
        f"# DeerFlow admin {label} credentials\n# This file is generated on first boot or password reset.\n# Change the password after login via Settings -> Account,\n# then delete this file.\n#\nemail: {email}\npassword: {password}\n"
    )

    # 原子 0600 创建或截断。使用 O_TRUNC（非 O_EXCL）使重置密码路径
    # 可以覆盖已有文件，无需单独的 unlink + create 操作。
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)

    return target.resolve()
