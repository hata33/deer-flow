"""
工具系统测试
"""
import pytest

from tools import get_tool_registry, bash_tool, read_file_tool, write_file_tool


def test_tool_registry():
    """测试工具注册表"""
    registry = get_tool_registry()

    # 内置工具应该已注册
    tool_names = registry.list_tools()
    assert "bash" in tool_names
    assert "read_file" in tool_names
    assert "write_file" in tool_names


def test_bash_tool():
    """测试 bash 工具"""
    result = bash_tool("echo 'Hello, World!'")
    assert "Hello, World!" in result


def test_write_and_read_file(tmp_path):
    """测试文件读写"""
    test_file = tmp_path / "test.txt"
    test_content = "This is a test content"

    # 写入文件
    result = write_file_tool(str(test_file), test_content)
    assert "成功" in result

    # 读取文件
    content = read_file_tool(str(test_file))
    assert content == test_content


def test_read_file_with_range(tmp_path):
    """测试读取文件范围"""
    test_file = tmp_path / "test.txt"
    test_content = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"

    write_file_tool(str(test_file), test_content)

    # 读取第2-3行
    content = read_file_tool(str(test_file), start_line=2, end_line=3)
    assert "Line 2" in content
    assert "Line 3" in content
    assert "Line 1" not in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
