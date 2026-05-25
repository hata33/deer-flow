"""密码哈希工具 — 版本化哈希格式。

本模块实现了 DeerFlow 的密码哈希与验证功能，使用版本化哈希格式
确保向后兼容性和渐进式安全升级。

哈希格式：``$dfv<N>$<bcrypt_hash>``，其中 ``<N>`` 是版本号。

版本说明：
  - **v1**（旧版）：``bcrypt(password)`` — 直接 bcrypt，受 72 字节
    静默截断限制影响
  - **v2**（当前）：``bcrypt(b64(sha256(password)))`` — SHA-256 预哈希
    避免 72 字节截断限制，完整密码参与哈希计算

核心设计：
  - 验证时自动检测哈希版本，无前缀的视为 v1（兼容版本化之前的数据）
  - 登录时机会性升级：检测到旧版哈希自动重新哈希（由 LocalAuthProvider 处理）
  - bcrypt 操作通过 asyncio.to_thread 包装为异步，避免阻塞事件循环
  - 格式错误的哈希返回 False 而非抛异常（失败即关闭策略）

使用方式：
  - 创建用户：hash_password(plain_password) → v2 哈希字符串
  - 验证密码：verify_password(plain_password, hashed_password) → bool
  - 检查是否需要重新哈希：needs_rehash(hashed_password) → bool
  - 异步版本：hash_password_async / verify_password_async
"""

import asyncio
import base64
import hashlib

import bcrypt

# 当前哈希版本号
_CURRENT_VERSION = 2
# v2 哈希前缀
_PREFIX_V2 = "$dfv2$"
# v1 哈希前缀
_PREFIX_V1 = "$dfv1$"


def _pre_hash_v2(password: str) -> bytes:
    """SHA-256 预哈希，绕过 bcrypt 的 72 字节限制。

    将密码先进行 SHA-256 哈希再 Base64 编码，确保任意长度
    密码的完整信息都参与 bcrypt 计算。

    Args:
        password: 明文密码。

    Returns:
        Base64 编码的 SHA-256 哈希字节。
    """
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    """哈希密码（当前版本：v2 — SHA-256 预哈希 + bcrypt）。

    Args:
        password: 明文密码。

    Returns:
        带版本前缀的哈希字符串（$dfv2$...）。
    """
    raw = bcrypt.hashpw(_pre_hash_v2(password), bcrypt.gensalt()).decode("utf-8")
    return f"{_PREFIX_V2}{raw}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码，自动检测哈希版本。

    支持三种格式：
      - v2（$dfv2$...）：SHA-256 预哈希 + bcrypt
      - v1（$dfv1$...）：直接 bcrypt
      - 无前缀：视为 v1（兼容版本化之前的数据）

    Args:
        plain_password: 待验证的明文密码。
        hashed_password: 存储的哈希字符串。

    Returns:
        密码匹配时返回 True，不匹配或哈希格式错误返回 False。
    """
    try:
        if hashed_password.startswith(_PREFIX_V2):
            # v2: SHA-256 预哈希后 bcrypt 验证
            bcrypt_hash = hashed_password[len(_PREFIX_V2) :]
            return bcrypt.checkpw(_pre_hash_v2(plain_password), bcrypt_hash.encode("utf-8"))

        if hashed_password.startswith(_PREFIX_V1):
            # v1: 直接 bcrypt 验证
            bcrypt_hash = hashed_password[len(_PREFIX_V1) :]
        else:
            # 无前缀：视为 v1（向后兼容）
            bcrypt_hash = hashed_password

        return bcrypt.checkpw(plain_password.encode("utf-8"), bcrypt_hash.encode("utf-8"))
    except ValueError:
        # bcrypt 对格式错误或损坏的哈希（如无效 salt）抛出 ValueError。
        # 返回 False（失败即关闭）而非让请求崩溃。
        return False


def needs_rehash(hashed_password: str) -> bool:
    """检查哈希是否使用旧版本，需要重新哈希。

    当哈希不是 v2 格式时返回 True，提示调用者在下次登录时
    使用当前版本重新哈希密码。

    Args:
        hashed_password: 存储的哈希字符串。

    Returns:
        True 表示需要重新哈希。
    """
    return not hashed_password.startswith(_PREFIX_V2)


async def hash_password_async(password: str) -> str:
    """异步哈希密码（非阻塞）。

    将阻塞的 bcrypt 操作包装在线程池中，避免阻塞事件循环。

    Args:
        password: 明文密码。

    Returns:
        带版本前缀的哈希字符串。
    """
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """异步验证密码（非阻塞）。

    将阻塞的 bcrypt 操作包装在线程池中，避免阻塞事件循环。

    Args:
        plain_password: 待验证的明文密码。
        hashed_password: 存储的哈希字符串。

    Returns:
        密码匹配时返回 True。
    """
    return await asyncio.to_thread(verify_password, plain_password, hashed_password)
