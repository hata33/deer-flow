"""动态模块解析器。

通过字符串路径（"module.path:variable_name" 格式）在运行时导入模块并获取变量，
支持类型校验和缺失依赖的可操作提示。
用于配置驱动的工具、模型、沙箱等组件的延迟加载。
"""

from importlib import import_module

# 已知 LangChain provider 模块到安装包名的映射表
# 当导入失败时，生成可操作的安装提示（如 "uv add langchain-google-genai"）
MODULE_TO_PACKAGE_HINTS = {
    "langchain_google_genai": "langchain-google-genai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_openai": "langchain-openai",
    "langchain_deepseek": "langchain-deepseek",
}


def _build_missing_dependency_hint(module_path: str, err: ImportError) -> str:
    """构建缺失依赖的可操作提示信息。

    优先从 MODULE_TO_PACKAGE_HINTS 查找已知的包名映射，
    未匹配时将模块名中的下划线转为连字符作为包名。

    Args:
        module_path: 原始导入路径。
        err: 原始 ImportError 异常。

    Returns:
        包含安装命令的提示字符串。
    """
    module_root = module_path.split(".", 1)[0]
    missing_module = getattr(err, "name", None) or module_root

    # 优先匹配已知 provider 包名，即使错误来自传递依赖（如 google 模块）
    package_name = MODULE_TO_PACKAGE_HINTS.get(module_root)
    if package_name is None:
        package_name = MODULE_TO_PACKAGE_HINTS.get(missing_module, missing_module.replace("_", "-"))

    return f"Missing dependency '{missing_module}'. Install it with `uv add {package_name}` (or `pip install {package_name}`), then restart DeerFlow."


def resolve_variable[T](
    variable_path: str,
    expected_type: type[T] | tuple[type, ...] | None = None,
) -> T:
    """通过字符串路径解析变量（如 "package.module:variable_name"）。

    核心反射函数，将配置文件中的字符串路径转换为运行时对象。
    用于工具、模型、沙箱等组件的延迟加载。

    Args:
        variable_path: 变量路径，格式为 "module.path:variable_name"。
        expected_type: 期望的类型或类型元组，用于 isinstance 校验。

    Returns:
        解析后的变量。

    Raises:
        ImportError: 模块路径无效或属性不存在，或依赖缺失。
        ValueError: 解析的变量不符合 expected_type 校验。
    """
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"{variable_path} doesn't look like a variable path. Example: parent_package_name.sub_package_name.module_name:variable_name") from err

    try:
        module = import_module(module_path)
    except ImportError as err:
        module_root = module_path.split(".", 1)[0]
        err_name = getattr(err, "name", None)
        if isinstance(err, ModuleNotFoundError) or err_name == module_root:
            hint = _build_missing_dependency_hint(module_path, err)
            raise ImportError(f"Could not import module {module_path}. {hint}") from err
        # 保留非缺失模块的原始错误信息
        raise ImportError(f"Error importing module {module_path}: {err}") from err

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise ImportError(f"Module {module_path} does not define a {variable_name} attribute/class") from err

    # 类型校验
    if expected_type is not None:
        if not isinstance(variable, expected_type):
            type_name = expected_type.__name__ if isinstance(expected_type, type) else " or ".join(t.__name__ for t in expected_type)
            raise ValueError(f"{variable_path} is not an instance of {type_name}, got {type(variable).__name__}")

    return variable


def resolve_class[T](class_path: str, base_class: type[T] | None = None) -> type[T]:
    """通过字符串路径解析类，并可选校验是否为指定基类的子类。

    Args:
        class_path: 类路径，格式为 "module_path:ClassName"。
        base_class: 可选的基类，用于 issubclass 校验。

    Returns:
        解析后的类对象。

    Raises:
        ImportError: 模块路径无效或属性不存在。
        ValueError: 解析的对象不是类，或不是 base_class 的子类。
    """
    model_class = resolve_variable(class_path, expected_type=type)

    if not isinstance(model_class, type):
        raise ValueError(f"{class_path} is not a valid class")

    if base_class is not None and not issubclass(model_class, base_class):
        raise ValueError(f"{class_path} is not a subclass of {base_class.__name__}")

    return model_class
