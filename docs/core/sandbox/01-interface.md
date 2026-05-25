# 抽象接口与本地实现

沙箱系统通过 `Sandbox` 抽象基类定义统一接口，`LocalSandbox` 作为本地文件系统实现完成全部方法。所有工具函数只依赖 `Sandbox` 接口，不直接引用具体实现类。

## Sandbox 抽象基类

定义于 `sandbox/sandbox.py`，是所有沙箱实现必须遵循的契约：

```python
class Sandbox(ABC):
    _id: str

    @abstractmethod
    def execute_command(self, command: str) -> str: ...

    @abstractmethod
    def read_file(self, path: str) -> str: ...

    @abstractmethod
    def download_file(self, path: str) -> bytes: ...

    @abstractmethod
    def list_dir(self, path: str, max_depth=2) -> list[str]: ...

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    @abstractmethod
    def glob(self, path: str, pattern: str, *,
             include_dirs: bool = False, max_results: int = 200
    ) -> tuple[list[str], bool]: ...

    @abstractmethod
    def grep(self, path: str, pattern: str, *,
             glob: str | None = None, literal: bool = False,
             case_sensitive: bool = False, max_results: int = 100
    ) -> tuple[list[GrepMatch], bool]: ...

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None: ...
```

方法说明：

| 方法 | 入参 | 返回值 | 用途 |
|------|------|--------|------|
| `execute_command` | bash 命令字符串 | stdout + stderr | 命令执行 |
| `read_file` | 绝对路径 | 文件文本内容 | 文件读取 |
| `download_file` | 绝对路径 | 原始字节 | 二进制文件下载（有 100MB 大小限制） |
| `list_dir` | 绝对路径, 最大深度 | 路径列表 | 目录列表（递归，含尾部 `/` 标记目录） |
| `write_file` | 路径, 内容, 追加标志 | 无 | 文件写入/追加（自动创建目录） |
| `glob` | 根路径, 模式 | (匹配路径列表, 是否截断) | 模式搜索 |
| `grep` | 根路径, 正则 | (GrepMatch列表, 是否截断) | 内容搜索 |
| `update_file` | 路径, 字节 | 无 | 二进制文件更新 |

## PathMapping 数据结构

定义于 `local/local_sandbox.py`，描述一条容器路径到本地路径的映射规则：

```python
@dataclass(frozen=True)
class PathMapping:
    container_path: str   # 容器/虚拟路径前缀
    local_path: str       # 宿主机物理路径前缀
    read_only: bool       # 是否只读（默认 False）
```

示例实例：
- `PathMapping("/mnt/user-data/workspace", "/home/user/.deer-flow/.../workspace", False)`
- `PathMapping("/mnt/skills", "/project/skills", True)` — 技能目录始终只读
- `PathMapping("/mnt/acp-workspace", "/home/user/.deer-flow/.../acp-workspace", False)`

## LocalSandbox 实现

`LocalSandbox` 继承 `Sandbox`，通过 `PathMapping` 列表将所有虚拟路径操作代理到宿主机文件系统。

### 路径解析链

正向解析（容器路径 → 本地路径）经过三级调用：

```
_resolve_path(path)
  └── _resolve_path_with_mapping(path)
        └── _find_path_mapping(path)
```

**`_find_path_mapping(path)`** — 按 `container_path` 长度降序匹配，找到最长前缀匹配的映射条目。返回 `(PathMapping, relative_part)` 或 `None`。

**`_resolve_path_with_mapping(path)`** — 调用 `_find_path_mapping` 后拼接 `local_path` + `relative_part`，解析为绝对路径，并验证结果仍在 `local_path` 边界内（防止路径逃逸）。返回 `ResolvedPath(path, mapping)` 命名元组。

**`_resolve_path(path)`** — 简写，只返回解析后的路径字符串。

路径解析示例：

```
输入: /mnt/user-data/workspace/src/main.py
  ↓ _find_path_mapping 匹配到 PathMapping("/mnt/user-data/workspace", "/home/user/.deer-flow/.../workspace")
  ↓ relative = "src/main.py"
  ↓ resolved = /home/user/.deer-flow/.../workspace/src/main.py
  ↓ relative_to() 校验通过
输出: ResolvedPath("/home/user/.deer-flow/.../workspace/src/main.py", mapping)
```

### 反向解析

`_reverse_resolve_path(path)` 将本地路径翻译回容器路径。按 `local_path` 长度降序匹配，替换前缀：

```
输入: /home/user/.deer-flow/.../workspace/src/main.py
  ↓ 匹配到 PathMapping("/mnt/user-data/workspace", "/home/user/.deer-flow/.../workspace")
  ↓ relative = "src/main.py"
输出: /mnt/user-data/workspace/src/main.py
```

### 前向路径解析（命令与内容）

除了工具参数的路径解析外，`LocalSandbox` 还对命令和文件内容中的容器路径进行批量替换：

**`_resolve_paths_in_command(command)`** — 使用正则表达式扫描命令字符串，将所有容器路径前缀替换为本地路径。按 `container_path` 长度降序构建正则，确保 `/mnt/user-data/workspace` 优先于 `/mnt/user-data` 匹配。边界检测使用 shell 元字符（空格、引号、分号等），避免误匹配。

**`_resolve_paths_in_content(content)`** — 与命令解析类似，但使用更宽松的边界检测（非 `\w./-` 字符），适用于任意文本内容。解析后的路径统一使用正斜杠，避免 Windows 反斜杠在源码中产生转义问题。

### Agent 写入路径追踪

`LocalSandbox` 维护 `_agent_written_paths: set[str]` 记录通过 `write_file` 写入的文件路径。`read_file` 只对这一集合中的文件执行反向路径解析——用户上传的文件、外部工具输出不会被静默改写。

```python
def write_file(self, path, content, append=False):
    resolved_content = self._resolve_paths_in_content(content)
    # ... 写入文件 ...
    self._agent_written_paths.add(resolved_path)  # 标记为 Agent 写入

def read_file(self, path):
    content = f.read()
    if resolved_path in self._agent_written_paths:
        content = self._reverse_resolve_paths_in_output(content)  # 仅 Agent 写入的文件才反向解析
    return content
```

### 输出中的路径反向解析

**`_reverse_resolve_paths_in_output(output)`** — 扫描输出字符串，用正则匹配所有本地路径前缀并替换为容器路径。对所有映射按 `local_path` 长度降序处理，确保更具体的路径优先匹配。此方法应用于：

- `execute_command()` 的命令输出
- `list_dir()` 的目录条目
- `glob()` 的匹配结果
- `grep()` 的文件路径（行内容保持原样）

### Shell 检测

`LocalSandbox._get_shell()` 按优先级自动检测可用的 Shell：

**Unix 系统**：`/bin/zsh` → `/bin/bash` → `/bin/sh` → `sh`（PATH 搜索）

**Windows 系统**：`pwsh` → `powershell` → `cmd.exe`

辅助方法：
- `_is_powershell(shell)` — 判断是否为 PowerShell（`pwsh` / `powershell`）
- `_is_cmd_shell(shell)` — 判断是否为 cmd.exe
- `_is_msys_shell(shell)` — 判断是否为 Git Bash/MSYS（需特殊环境变量避免路径转换）
- `_find_first_available_shell(candidates)` — 从候选列表返回第一个可执行的 Shell

`execute_command()` 根据 Shell 类型构建不同的命令参数：
- Unix Shell / MSYS: `[shell, "-c", command]`
- PowerShell: `[shell, "-NoProfile", "-Command", command]`
- cmd.exe: `[shell, "/c", command]`

MSYS Shell 额外设置 `MSYS_NO_PATHCONV=1` 和 `MSYS2_ARG_CONV_EXCL=*` 防止自动路径转换。

## 目录列表实现

`list_dir.py` 提供 `list_dir(path, max_depth)` 函数，递归遍历目录树：

- 使用 `should_ignore_name()` 过滤 `.git`、`node_modules`、`__pycache__` 等噪声目录
- 解析符号链接后验证仍在根目录范围内（防止符号链接逃逸）
- 目录条目附加尾部 `/` 标记
- 最终结果按字母排序

## ResolvedPath 命名元组

```python
class ResolvedPath(NamedTuple):
    path: str              # 解析后的本地绝对路径
    mapping: PathMapping | None  # 匹配的映射条目（可能为 None）
```

用于在解析过程中同时返回路径和映射信息，支持只读检查等需要映射元数据的操作。
