"""凭证自动加载器 — 从 Claude Code CLI 和 Codex CLI 自动加载认证凭证。

模块功能
========
自动从本地 CLI 工具的凭证存储中加载 OAuth Token，实现"无需手动配置 API Key"
的开发体验。支持两种凭证策略：

1. **Claude Code CLI OAuth Token**
   - 使用 `Authorization: Bearer` 头认证（而非 `x-api-key`）
   - 需要附加 anthropic-beta 头：`oauth-2025-04-20,claude-code-20250219`
   - 支持多种加载源：环境变量、文件描述符、凭证文件

2. **Codex CLI Token**
   - 使用 chatgpt.com/backend-api/codex/responses 端点
   - 支持旧版顶层 token 格式和新版嵌套 tokens 格式
   - 默认从 ~/.codex/auth.json 加载

核心设计
========
1. **多源回退**: 按优先级依次尝试多个凭证来源，第一个成功即返回
2. **过期检测**: 自动检查 OAuth Token 的过期时间，过期凭证不返回
3. **文件描述符支持**: 通过 Linux 文件描述符传递敏感 Token，避免命令行泄露
4. **路径可配置**: 支持通过环境变量自定义凭证文件路径

凭证加载优先级
==============
Claude Code 凭证（按优先级排序）：
  1. $CLAUDE_CODE_OAUTH_TOKEN 或 $ANTHROPIC_AUTH_TOKEN（直接环境变量）
  2. $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR（文件描述符读取）
  3. $CLAUDE_CODE_CREDENTIALS_PATH（自定义凭证路径）
  4. ~/.claude/.credentials.json（默认凭证文件）

Codex CLI 凭证：
  1. $CODEX_AUTH_PATH（自定义路径）
  2. ~/.codex/auth.json（默认路径）

使用场景
========
- 在本地开发环境中，直接使用 Claude Code CLI 的登录态访问 API
- 在 CI/CD 环境中，通过环境变量或文件描述符传递临时凭证
- 在容器化部署中，挂载凭证文件实现无 Key 配置

注意事项
========
- OAuth Token 有效期有限，过期后需重新运行 `claude` 命令刷新
- 文件描述符方式仅在 Unix-like 系统上可用
- 凭证文件包含敏感信息，应设置适当的文件权限（如 600）
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Claude Code OAuth Token 所需的 beta 头信息
# 包含三个能力标志：OAuth 认证协议、Claude Code 工具链、交错思维模式
OAUTH_ANTHROPIC_BETAS = "oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14"


def is_oauth_token(token: str) -> bool:
    """检测给定的 Token 是否为 Claude Code OAuth Token。

    通过前缀 `sk-ant-oat` 来区分 OAuth Token 和标准 API Key。
    标准 API Key 使用 `sk-ant-api` 前缀。

    Args:
        token: 待检测的 Token 字符串。

    Returns:
        bool: 如果是 OAuth Token 返回 True，否则返回 False。
    """
    return isinstance(token, str) and "sk-ant-oat" in token


@dataclass
class ClaudeCodeCredential:
    """Claude Code CLI 的 OAuth 凭证数据类。

    封装了从 Claude Code CLI 获取的 OAuth 凭证信息，包括访问令牌、
    刷新令牌和过期时间。

    Attributes:
        access_token: OAuth 访问令牌（以 sk-ant-oat 开头）。
        refresh_token: OAuth 刷新令牌（以 sk-ant-ort 开头）。
        expires_at: 令牌过期时间（Unix 毫秒时间戳）。
        source: 凭证来源标识（如 'claude-cli-env', 'claude-cli-file'）。
    """

    access_token: str
    refresh_token: str = ""
    expires_at: int = 0
    source: str = ""

    @property
    def is_expired(self) -> bool:
        """检查 OAuth Token 是否已过期。

        考虑 1 分钟的缓冲时间，避免在请求过程中恰好过期。

        Returns:
            bool: 如果已过期（或即将在 1 分钟内过期）返回 True。
        """
        if self.expires_at <= 0:
            # 没有过期时间信息，视为永不过期
            return False
        return time.time() * 1000 > self.expires_at - 60_000  # 1 分钟缓冲


@dataclass
class CodexCliCredential:
    """Codex CLI 的凭证数据类。

    封装了从 Codex CLI 获取的认证信息。

    Attributes:
        access_token: 访问令牌。
        account_id: 账户 ID（用于 ChatGPT-Account-ID 请求头）。
        source: 凭证来源标识（如 'codex-cli'）。
    """

    access_token: str
    account_id: str = ""
    source: str = ""


def _resolve_credential_path(env_var: str, default_relative_path: str) -> Path:
    """解析凭证文件路径：优先使用环境变量，否则使用默认路径。

    Args:
        env_var: 环境变量名，用于指定自定义凭证文件路径。
        default_relative_path: 相对于用户主目录的默认凭证文件路径。

    Returns:
        Path: 解析后的凭证文件绝对路径。
    """
    configured_path = os.getenv(env_var)
    if configured_path:
        return Path(configured_path).expanduser()
    return _home_dir() / default_relative_path


def _home_dir() -> Path:
    """获取用户主目录路径。

    优先使用 HOME 环境变量，这在某些容器环境中是必需的。

    Returns:
        Path: 用户主目录路径。
    """
    home = os.getenv("HOME")
    if home:
        return Path(home).expanduser()
    return Path.home()


def _load_json_file(path: Path, label: str) -> dict[str, Any] | None:
    """安全加载 JSON 文件，处理各种异常情况。

    Args:
        path: JSON 文件路径。
        label: 文件标签（用于日志输出）。

    Returns:
        dict | None: 解析成功返回字典，失败返回 None。
    """
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
    """通过文件描述符读取敏感信息。

    在 Unix-like 系统中，可以通过文件描述符传递敏感信息（如进程间通信），
    避免在命令行参数或环境变量中暴露明文。

    Args:
        env_var: 存储文件描述符编号的环境变量名。

    Returns:
        str | None: 读取成功返回内容字符串，失败返回 None。
    """
    fd_value = os.getenv(env_var)
    if not fd_value:
        return None

    try:
        fd = int(fd_value)
    except ValueError:
        logger.warning(f"{env_var} must be an integer file descriptor, got: {fd_value}")
        return None

    try:
        # 最多读取 1MB，防止意外读取超大文件
        secret = os.read(fd, 1024 * 1024).decode().strip()
    except OSError as e:
        logger.warning(f"Failed to read {env_var}: {e}")
        return None

    return secret or None


def _credential_from_direct_token(access_token: str, source: str) -> ClaudeCodeCredential | None:
    """从直接提供的 Token 字符串创建凭证对象。

    Args:
        access_token: 访问令牌字符串（会被去除首尾空白）。
        source: 凭证来源标识。

    Returns:
        ClaudeCodeCredential | None: Token 非空时返回凭证对象，否则返回 None。
    """
    token = access_token.strip()
    if not token:
        return None
    return ClaudeCodeCredential(access_token=token, source=source)


def _iter_claude_code_credential_paths() -> list[Path]:
    """获取 Claude Code 凭证文件的候选路径列表。

    如果设置了 CLAUDE_CODE_CREDENTIALS_PATH 环境变量，优先使用该路径，
    同时也会包含默认的 ~/.claude/.credentials.json 路径作为后备。

    Returns:
        list[Path]: 凭证文件路径列表，按优先级排序。
    """
    paths: list[Path] = []
    override_path = os.getenv("CLAUDE_CODE_CREDENTIALS_PATH")
    if override_path:
        paths.append(Path(override_path).expanduser())

    default_path = _home_dir() / ".claude/.credentials.json"
    # 避免重复添加同一路径
    if not paths or paths[-1] != default_path:
        paths.append(default_path)

    return paths


def _extract_claude_code_credential(data: dict[str, Any], source: str) -> ClaudeCodeCredential | None:
    """从 JSON 数据中提取 Claude Code OAuth 凭证。

    凭证文件格式::

        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-...",
                "refreshToken": "sk-ant-ort01-...",
                "expiresAt": 1773430695128,
                "scopes": ["user:inference", ...]
            }
        }

    Args:
        data: 解析后的 JSON 字典数据。
        source: 凭证来源标识。

    Returns:
        ClaudeCodeCredential | None: 提取成功且未过期返回凭证对象，否则返回 None。
    """
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

    # 过期的 Token 不返回，避免后续请求失败
    if cred.is_expired:
        logger.warning("Claude Code OAuth token is expired. Run 'claude' to refresh.")
        return None

    return cred


def load_claude_code_credential() -> ClaudeCodeCredential | None:
    """从 Claude Code CLI 的各种凭证源加载 OAuth 凭证。

    按以下优先级依次尝试：
      1. $CLAUDE_CODE_OAUTH_TOKEN 或 $ANTHROPIC_AUTH_TOKEN（直接环境变量）
      2. $CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR（文件描述符）
      3. $CLAUDE_CODE_CREDENTIALS_PATH（自定义路径）
      4. ~/.claude/.credentials.json（默认凭证文件）

    凭证文件 JSON 格式::

        {
          "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-...",
            "refreshToken": "sk-ant-ort01-...",
            "expiresAt": 1773430695128,
            "scopes": ["user:inference", ...]
          }
        }

    Returns:
        ClaudeCodeCredential | None: 成功加载返回凭证对象，所有源均失败返回 None。
    """
    # 优先级 1：直接从环境变量获取
    direct_token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if direct_token:
        cred = _credential_from_direct_token(direct_token, "claude-cli-env")
        if cred:
            logger.info("Loaded Claude Code OAuth credential from environment")
        return cred

    # 优先级 2：从文件描述符读取
    fd_token = _read_secret_from_file_descriptor("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")
    if fd_token:
        cred = _credential_from_direct_token(fd_token, "claude-cli-fd")
        if cred:
            logger.info("Loaded Claude Code OAuth credential from file descriptor")
        return cred

    # 优先级 3 & 4：从凭证文件加载
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
    """从 Codex CLI 的认证文件加载凭证。

    凭证文件默认位于 ~/.codex/auth.json，支持两种格式：
    - 旧版：顶层 `access_token` / `token` / `account_id`
    - 新版：嵌套在 `tokens` 对象中

    可通过 $CODEX_AUTH_PATH 环境变量自定义文件路径。

    Returns:
        CodexCliCredential | None: 成功加载返回凭证对象，失败返回 None。
    """
    cred_path = _resolve_credential_path("CODEX_AUTH_PATH", ".codex/auth.json")
    data = _load_json_file(cred_path, "Codex CLI credentials")
    if data is None:
        return None
    # 兼容新旧两种 token 存储格式
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        tokens = {}

    # 按优先级尝试不同的 token 字段名
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
