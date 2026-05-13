"""Claude Code CLI 和 Codex CLI 的凭据自动加载。

实现两种凭据策略：
  1. Claude Code OAuth token（支持环境变量、文件描述符、凭据文件）
     - 使用 Authorization: Bearer 头（非 x-api-key）
     - 需要 anthropic-beta: oauth-2025-04-20,claude-code-20250219
     - 查找顺序：$CLAUDE_CODE_OAUTH_TOKEN → $ANTHROPIC_AUTH_TOKEN →
       $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR →
       $CLAUDE_CODE_CREDENTIALS_PATH → ~/.claude/.credentials.json
  2. Codex CLI token（从 ~/.codex/auth.json 加载）
     - 支持 legacy 顶层 token 和当前嵌套 token 结构
     - 覆盖路径：$CODEX_AUTH_PATH
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Claude Code OAuth token 所需的 beta 头
OAUTH_ANTHROPIC_BETAS = "oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14"


def is_oauth_token(token: str) -> bool:
    """判断 token 是否为 Claude Code OAuth token（包含 sk-ant-oat 前缀）。"""
    return isinstance(token, str) and "sk-ant-oat" in token


@dataclass
class ClaudeCodeCredential:
    """Claude Code CLI OAuth 凭据。

    Attributes:
        access_token: 访问令牌。
        refresh_token: 刷新令牌。
        expires_at: 过期时间（毫秒时间戳）。
        source: 凭据来源标识。
    """

    access_token: str
    refresh_token: str = ""
    expires_at: int = 0
    source: str = ""

    @property
    def is_expired(self) -> bool:
        """检查 token 是否已过期（含 1 分钟缓冲）。"""
        if self.expires_at <= 0:
            return False
        return time.time() * 1000 > self.expires_at - 60_000


@dataclass
class CodexCliCredential:
    """Codex CLI 凭据。

    Attributes:
        access_token: 访问令牌。
        account_id: 账户 ID。
        source: 凭据来源标识。
    """

    access_token: str
    account_id: str = ""
    source: str = ""


def _resolve_credential_path(env_var: str, default_relative_path: str) -> Path:
    """解析凭据文件路径，优先使用环境变量指定的路径。"""
    configured_path = os.getenv(env_var)
    if configured_path:
        return Path(configured_path).expanduser()
    return _home_dir() / default_relative_path


def _home_dir() -> Path:
    """获取用户主目录。"""
    home = os.getenv("HOME")
    if home:
        return Path(home).expanduser()
    return Path.home()


def _load_json_file(path: Path, label: str) -> dict[str, Any] | None:
    """安全加载 JSON 文件，文件不存在或格式错误时返回 None。"""
    if not path.exists():
        logger.debug(f"{label} not found: {path}")
        return None
    if path.is_dir():
        logger.warning(f"{label} path is a directory, expected a file: {path}")
        return None

    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {label}: {e}")
        return None


def _read_secret_from_file_descriptor(env_var: str) -> str | None:
    """从文件描述符读取密钥（支持 Claude Code 的 FD 传递机制）。"""
    fd_value = os.getenv(env_var)
    if not fd_value:
        return None

    try:
        fd = int(fd_value)
    except ValueError:
        logger.warning(f"{env_var} must be an integer file descriptor, got: {fd_value}")
        return None

    try:
        secret = os.read(fd, 1024 * 1024).decode().strip()
    except OSError as e:
        logger.warning(f"Failed to read {env_var}: {e}")
        return None

    return secret or None


def _credential_from_direct_token(access_token: str, source: str) -> ClaudeCodeCredential | None:
    """从直接传入的 token 字符串创建凭据。"""
    token = access_token.strip()
    if not token:
        return None
    return ClaudeCodeCredential(access_token=token, source=source)


def _iter_claude_code_credential_paths() -> list[Path]:
    """生成 Claude Code 凭据文件的搜索路径列表。"""
    paths: list[Path] = []
    override_path = os.getenv("CLAUDE_CODE_CREDENTIALS_PATH")
    if override_path:
        paths.append(Path(override_path).expanduser())

    default_path = _home_dir() / ".claude/.credentials.json"
    if not paths or paths[-1] != default_path:
        paths.append(default_path)

    return paths


def _extract_claude_code_credential(data: dict[str, Any], source: str) -> ClaudeCodeCredential | None:
    """从凭据文件内容中提取 Claude Code OAuth 凭据。"""
    oauth = data.get("claudeAiOauth", {})
    access_token = oauth.get("accessToken", "")
    if not access_token:
        logger.debug("Claude Code credentials container exists but no accessToken found")
        return None

    cred = ClaudeCodeCredential(
        access_token=access_token,
        refresh_token=oauth.get("refreshToken", ""),
        expires_at=oauth.get("expiresAt", 0),
        source=source,
    )

    if cred.is_expired:
        logger.warning("Claude Code OAuth token is expired. Run 'claude' to refresh.")
        return None

    return cred


def load_claude_code_credential() -> ClaudeCodeCredential | None:
    """从多个来源加载 Claude Code OAuth 凭据。

    查找顺序：
    1. $CLAUDE_CODE_OAUTH_TOKEN 或 $ANTHROPIC_AUTH_TOKEN（直接 token）
    2. $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR（文件描述符）
    3. $CLAUDE_CODE_CREDENTIALS_PATH（自定义路径）
    4. ~/.claude/.credentials.json（默认路径）

    Returns:
        ClaudeCodeCredential 实例，未找到时返回 None。
    """
    # 直接 token（环境变量）
    direct_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if direct_token:
        cred = _credential_from_direct_token(direct_token, "claude-cli-env")
        if cred:
            logger.info("Loaded Claude Code OAuth credential from environment")
        return cred

    # 文件描述符
    fd_token = _read_secret_from_file_descriptor("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")
    if fd_token:
        cred = _credential_from_direct_token(fd_token, "claude-cli-fd")
        if cred:
            logger.info("Loaded Claude Code OAuth credential from file descriptor")
        return cred

    # 凭据文件（自定义路径或默认路径）
    override_path = os.getenv("CLAUDE_CODE_CREDENTIALS_PATH")
    override_path_obj = Path(override_path).expanduser() if override_path else None
    for cred_path in _iter_claude_code_credential_paths():
        data = _load_json_file(cred_path, "Claude Code credentials")
        if data is None:
            continue
        cred = _extract_claude_code_credential(data, "claude-cli-file")
        if cred:
            source_label = "override path" if override_path_obj is not None and cred_path == override_path_obj else "plaintext file"
            logger.info(f"Loaded Claude Code OAuth credential from {source_label} (expires_at={cred.expires_at})")
            return cred

    return None


def load_codex_cli_credential() -> CodexCliCredential | None:
    """从 Codex CLI 的 auth.json 加载凭据（~/.codex/auth.json）。

    支持 legacy 顶层 token（access_token/token）和当前嵌套结构（tokens.access_token）。

    Returns:
        CodexCliCredential 实例，未找到时返回 None。
    """
    cred_path = _resolve_credential_path("CODEX_AUTH_PATH", ".codex/auth.json")
    data = _load_json_file(cred_path, "Codex CLI credentials")
    if data is None:
        return None
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        tokens = {}

    access_token = data.get("access_token") or data.get("token") or tokens.get("access_token", "")
    account_id = data.get("account_id") or tokens.get("account_id", "")
    if not access_token:
        logger.debug("Codex CLI credentials file exists but no token found")
        return None

    logger.info("Loaded Codex CLI credential")
    return CodexCliCredential(
        access_token=access_token,
        account_id=account_id,
        source="codex-cli",
    )
