"""CLI 管理员密码重置工具 — 安全地重置管理员密码。

本模块提供了命令行工具，用于在无法登录时重置管理员密码。

使用方式：
    python -m app.gateway.auth.reset_admin
    python -m app.gateway.auth.reset_admin --email admin@example.com

安全设计：
  - 新密码写入 .deer-flow/admin_initial_credentials.txt（0600 权限），
    而非打印到标准输出，确保 CI/日志聚合器不会泄露明文密码
  - 密码重置后用户 token_version 递增，使所有已登录会话失效
  - needs_setup 标志设为 True，强制用户下次登录时设置新密码

执行流程：
  1. 加载应用配置和持久化引擎
  2. 查找目标管理员（按邮箱或第一个管理员）
  3. 生成随机密码（16 字节 URL-safe）
  4. 哈希密码、递增 token_version、设置 needs_setup
  5. 更新数据库并写入凭据文件
  6. 关闭持久化引擎

退出码：
  - 0: 重置成功
  - 1: 用户不存在或持久化引擎不可用
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import select

from app.gateway.auth.credential_file import write_initial_credentials
from app.gateway.auth.password import hash_password
from app.gateway.auth.repositories.sqlite import SQLiteUserRepository
from deerflow.persistence.user.model import UserRow


async def _run(email: str | None) -> int:
    """执行密码重置的核心逻辑。

    Args:
        email: 指定的管理员邮箱，None 表示重置第一个找到的管理员。

    Returns:
        退出码（0=成功，1=失败）。
    """
    from deerflow.config import get_app_config
    from deerflow.persistence.engine import (
        close_engine,
        get_session_factory,
        init_engine_from_config,
    )

    config = get_app_config()
    await init_engine_from_config(config.database)
    try:
        sf = get_session_factory()
        if sf is None:
            print("Error: persistence engine not available (check config.database).", file=sys.stderr)
            return 1

        repo = SQLiteUserRepository(sf)

        if email:
            # 按邮箱查找指定用户
            user = await repo.get_user_by_email(email)
        else:
            # 查找第一个管理员 — 通过直接 SELECT 实现，因为仓库不暴露
            # "第一个管理员"辅助方法，我们不希望仅为这个 CLI 添加一个。
            async with sf() as session:
                stmt = select(UserRow).where(UserRow.system_role == "admin").limit(1)
                row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                user = None
            else:
                user = await repo.get_user_by_id(row.id)

        if user is None:
            if email:
                print(f"Error: user '{email}' not found.", file=sys.stderr)
            else:
                print("Error: no admin user found.", file=sys.stderr)
            return 1

        # 生成随机密码并更新用户
        new_password = secrets.token_urlsafe(16)
        user.password_hash = hash_password(new_password)
        # 递增 token_version 使所有已登录会话失效
        user.token_version += 1
        # 强制用户下次登录时设置新密码
        user.needs_setup = True
        await repo.update_user(user)

        # 安全写入凭据文件（0600 权限）
        cred_path = write_initial_credentials(user.email, new_password, label="reset")
        print(f"Password reset for: {user.email}")
        print(f"Credentials written to: {cred_path} (mode 0600)")
        print("Next login will require setup (new email + password).")
        return 0
    finally:
        await close_engine()


def main() -> None:
    """CLI 入口函数。

    解析命令行参数并调用异步重置逻辑。
    """
    parser = argparse.ArgumentParser(description="Reset admin password")
    parser.add_argument("--email", help="Admin email (default: first admin found)")
    args = parser.parse_args()

    exit_code = asyncio.run(_run(args.email))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
