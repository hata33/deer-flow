"""目录递归遍历工具 —— 列出指定目录下的文件和子目录。

本模块提供了 :func:`list_dir` 函数，用于递归遍历目录树并返回所有文件和
子目录的绝对路径。该函数被 :class:`LocalSandbox` 的 ``list_dir`` 方法调用。

特性
~~~~
- 支持自定义递归深度（max_depth）
- 自动跳过匹配 IGNORE_PATTERNS 的文件和目录
- 安全处理符号链接：解析后检查是否仍在根目录内，防止逃逸
- 目录路径以 ``/`` 后缀标识
- 返回结果按字典序排序
"""

from pathlib import Path

from deerflow.sandbox.search import should_ignore_name


def list_dir(path: str, max_depth: int = 2) -> list[str]:
    """列出指定目录下的文件和目录，递归到指定深度。

    从给定路径开始遍历，收集所有文件和子目录的绝对路径。
    结果中目录路径以 ``/`` 结尾作为标识，便于调用方区分文件和目录。

    安全处理符号链接：
    - 符号链接会被解析为真实路径
    - 如果解析后的路径超出了根目录范围，该链接会被跳过
    - 这防止了通过符号链接逃逸到沙箱外部

    Args:
        path: 要列出的根目录路径（宿主机真实路径，已经过 resolve）。
        max_depth: 最大递归深度（默认 2）。
            1 = 仅列出直接子项
            2 = 列出子项和孙项
            以此类推。

    Returns:
        排序后的绝对路径列表。目录路径以 ``/`` 后缀标识。
        如果路径不存在或不是目录，返回空列表。
    """
    result: list[str] = []
    root_path = Path(path).resolve()

    # 如果路径不存在或不是目录，返回空列表
    if not root_path.is_dir():
        return result

    def _is_within_root(candidate: Path) -> bool:
        """检查候选路径是否在根目录范围内。

        通过 ``relative_to`` 方法判断：如果能计算出相对路径，
        说明候选路径在根目录内。

        Args:
            candidate: 待检查的路径（已 resolve）。

        Returns:
            如果在根目录内返回 True，否则返回 False。
        """
        try:
            candidate.relative_to(root_path)
            return True
        except ValueError:
            # relative_to 抛出 ValueError 表示不在根目录内
            return False

    def _traverse(current_path: Path, current_depth: int) -> None:
        """递归遍历目录树。

        从 current_path 开始，收集文件和目录路径到 result 中。
        递归深入直到达到 max_depth 限制。

        Args:
            current_path: 当前遍历的目录路径。
            current_depth: 当前递归深度（1 表示根目录的直接子项）。
        """
        # 超过最大深度，停止递归
        if current_depth > max_depth:
            return

        try:
            for item in current_path.iterdir():
                # 跳过匹配忽略模式的文件/目录名
                if should_ignore_name(item.name):
                    continue

                # 处理符号链接：解析为真实路径并检查是否在根目录内
                if item.is_symlink():
                    try:
                        item_resolved = item.resolve()
                        if not _is_within_root(item_resolved):
                            # 符号链接指向根目录外，跳过以防止逃逸
                            continue
                    except OSError:
                        # 符号链接解析失败（如目标不存在），跳过
                        continue
                    # 目录路径添加 "/" 后缀
                    post_fix = "/" if item_resolved.is_dir() else ""
                    result.append(str(item_resolved) + post_fix)
                    continue

                # 普通文件/目录：resolve 后检查范围
                item_resolved = item.resolve()
                if not _is_within_root(item_resolved):
                    continue

                # 目录路径添加 "/" 后缀
                post_fix = "/" if item.is_dir() else ""
                result.append(str(item_resolved) + post_fix)

                # 如果是目录且未达到最大深度，递归进入子目录
                if item.is_dir() and current_depth < max_depth:
                    _traverse(item, current_depth + 1)
        except PermissionError:
            # 无权限访问的目录，静默跳过
            pass

    _traverse(root_path, 1)

    return sorted(result)
