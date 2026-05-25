"""动态模块解析器 —— 基于 importlib 的延迟加载与依赖提示。

本文件实现了 DeerFlow 反射模块的核心功能：将 ``"package.module:attr"``
格式的路径字符串解析为实际的 Python 对象。这是整个系统"配置驱动实例化"
的基础设施层。

核心设计思路：
    - **路径格式** —— 采用 ``模块路径:属性名`` 的冒号分隔格式
      （如 ``"langchain_openai:ChatOpenAI"``），与 Flask/Django 等框架的
      dotted-path 约定类似，便于在配置文件中书写。
    - **延迟导入** —— 仅在调用 ``resolve_*`` 函数时才触发 import，
      避免启动时加载所有可能的集成包，显著减少冷启动时间和内存占用。
    - **依赖提示** —— 当 ``importlib.import_module`` 抛出 ``ImportError``
      时，自动匹配 ``MODULE_TO_PACKAGE_HINTS`` 映射表，生成包含正确
      ``uv add`` / ``pip install`` 命令的错误消息，让用户无需查阅文档
      即可安装缺失的依赖。

模块级常量：
    MODULE_TO_PACKAGE_HINTS (dict[str, str]):
        将 Python import 名称映射到对应的 PyPI 包名。
        LangChain 生态的 import 名使用下划线（如 ``langchain_openai``），
        而 PyPI 包名使用连字符（如 ``langchain-openai``），此映射表消除
        了这一差异。

异常策略：
    - 路径格式错误 → ``ImportError``（附使用示例）
    - 模块不存在 → ``ImportError``（附安装提示）
    - 属性不存在 → ``ImportError``（明确指出缺失的属性名）
    - 类型不匹配 → ``ValueError``（附期望类型与实际类型）
"""

from importlib import import_module

# 已知 LangChain 集成包的模块名 → PyPI 包名映射。
# import 名使用下划线（Python 约定），pip/uv 包名使用连字符（PyPI 约定）。
# 当 import 失败时，此表用于生成准确的安装提示命令。
MODULE_TO_PACKAGE_HINTS = {
    "langchain_google_genai": "langchain-google-genai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_openai": "langchain-openai",
    "langchain_deepseek": "langchain-deepseek",
}


def _build_missing_dependency_hint(module_path: str, err: ImportError) -> str:
    """根据模块路径和导入错误构建可操作的安装提示。

    当模块导入失败时，尝试从映射表中查找对应的 PyPI 包名，
    生成包含 ``uv add`` 和 ``pip install`` 命令的提示信息。
    即使错误是由传递依赖（如 ``google`` 模块）触发的，
    也会优先使用已知集成包的提示，而非直接暴露底层依赖名。

    Args:
        module_path: 完整的模块路径（如 ``"langchain_google_genai.chat_models"``）。
        err: 原始的 ``ImportError`` 异常实例。

    Returns:
        包含安装命令的提示字符串，例如：
        ``"Missing dependency 'langchain_openai'. Install it with `uv add langchain-openai` ..."``

    Note:
        此函数不会抛出异常，始终返回一个字符串。
    """
    # 取路径的第一个段作为模块根名（如 "langchain_google_genai"）
    module_root = module_path.split(".", 1)[0]
    # 优先使用异常中携带的模块名，回退到模块根名
    missing_module = getattr(err, "name", None) or module_root

    # 优先使用已知集成包的映射提示，因为用户通常需要安装的是上层集成包
    # 而非底层的传递依赖（如 google-ai-generativelanguage）
    package_name = MODULE_TO_PACKAGE_HINTS.get(module_root)
    if package_name is None:
        # 未找到直接映射时，尝试用异常中的模块名匹配；
        # 兜底策略是将下划线替换为连字符（Python → PyPI 命名转换）
        package_name = MODULE_TO_PACKAGE_HINTS.get(missing_module, missing_module.replace("_", "-"))

    return f"Missing dependency '{missing_module}'. Install it with `uv add {package_name}` (or `pip install {package_name}`), then restart DeerFlow."


def resolve_variable[T](
    variable_path: str,
    expected_type: type[T] | tuple[type, ...] | None = None,
) -> T:
    """从路径字符串解析 Python 变量。

    将 ``"package.module:variable_name"`` 格式的路径解析为实际的 Python 对象。
    这是整个反射模块的基础函数，``resolve_class`` 也依赖它完成核心解析。

    解析流程：
        1. 以最后一个冒号为界，拆分为模块路径和属性名。
        2. 使用 ``importlib.import_module`` 动态导入模块。
        3. 通过 ``getattr`` 获取模块上的属性。
        4. （可选）使用 ``isinstance`` 校验类型。

    Args:
        variable_path: 变量的路径字符串，格式为
            ``"父包名.子包名.模块名:变量名"``。
            冒号左侧是 Python 模块的 dotted path，
            右侧是模块顶层的一个属性名。
            示例：``"langchain_openai:ChatOpenAI"``。
        expected_type: 可选的类型约束。可以是单个 type 或 type 元组。
            传入后，使用 ``isinstance()`` 校验解析结果是否为该类型的实例。
            不匹配时抛出 ``ValueError``。

    Returns:
        解析到的 Python 对象，类型由 ``expected_type`` 泛型参数约束。

    Raises:
        ImportError: 路径格式不合法（缺少冒号）、模块无法导入、
            或模块上不存在指定属性。
        ValueError: 解析到的对象不满足 ``expected_type`` 约束。

    Example::

        # 加载一个模型类实例
        cls = resolve_variable("langchain_openai:ChatOpenAI", expected_type=type)
        instance = cls(model="gpt-4o")
    """
    try:
        # 以最后一个冒号分割，左侧为模块路径，右侧为属性名
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        # rsplit 在没有冒号时抛出 ValueError，转换为更友好的 ImportError
        raise ImportError(f"{variable_path} doesn't look like a variable path. Example: parent_package_name.sub_package_name.module_name:variable_name") from err

    try:
        module = import_module(module_path)
    except ImportError as err:
        # 区分"模块不存在"和"模块内部错误"两种情况
        module_root = module_path.split(".", 1)[0]
        err_name = getattr(err, "name", None)
        if isinstance(err, ModuleNotFoundError) or err_name == module_root:
            # 模块确实不存在，生成带安装提示的错误消息
            hint = _build_missing_dependency_hint(module_path, err)
            raise ImportError(f"Could not import module {module_path}. {hint}") from err
        # 模块存在但内部 import 链路出错，保留原始错误信息
        raise ImportError(f"Error importing module {module_path}: {err}") from err

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        # 模块已加载但不存在指定属性（如拼写错误的类名）
        raise ImportError(f"Module {module_path} does not define a {variable_name} attribute/class") from err

    # 类型校验：确保解析结果符合调用者的期望类型
    if expected_type is not None:
        if not isinstance(variable, expected_type):
            # 构建可读的类型名称字符串，支持单类型和元组类型
            type_name = expected_type.__name__ if isinstance(expected_type, type) else " or ".join(t.__name__ for t in expected_type)
            raise ValueError(f"{variable_path} is not an instance of {type_name}, got {type(variable).__name__}")

    return variable


def resolve_class[T](class_path: str, base_class: type[T] | None = None) -> type[T]:
    """从路径字符串解析 Python 类，并可校验继承关系。

    这是 ``resolve_variable`` 的高层封装，专门用于解析**类对象**
    （而非实例）。解析后会额外检查：
        1. 结果是否为 ``type``（即是否是一个类）。
        2. （可选）是否是 ``base_class`` 的子类。

    该函数是 DeerFlow 配置驱动实例化的核心入口：从配置文件中读取
    类路径字符串，动态加载对应的类，然后实例化使用。

    Args:
        class_path: 类的路径字符串，格式为 ``"模块路径:类名"``。
            示例：``"langchain_openai:ChatOpenAI"``。
        base_class: 可选的基类约束。传入后，使用 ``issubclass()``
            校验解析到的类是否为该基类的子类。不满足时抛出 ``ValueError``。

    Returns:
        解析到的类对象（注意是类本身，而非实例）。

    Raises:
        ImportError: 路径格式不合法、模块无法导入、或模块上不存在指定属性。
        ValueError: 解析到的对象不是类，或不是 ``base_class`` 的子类。

    Example::

        from langchain_core.language_models import BaseChatModel

        # 从配置读取类路径，动态加载并校验基类
        LLMClass = resolve_class("langchain_openai:ChatOpenAI", base_class=BaseChatModel)
        llm = LLMClass(model="gpt-4o")
    """
    # 先解析变量，同时校验是否为 type（即类对象）
    model_class = resolve_variable(class_path, expected_type=type)

    # 二次确认：resolve_variable 的 isinstance 检查足以过滤非类型对象，
    # 但显式检查 type 更清晰，也为未来 Python 版本的类型系统变化留出余量
    if not isinstance(model_class, type):
        raise ValueError(f"{class_path} is not a valid class")

    # 基类校验：确保解析到的类可以通过 issubclass 检查
    if base_class is not None and not issubclass(model_class, base_class):
        raise ValueError(f"{class_path} is not a subclass of {base_class.__name__}")

    return model_class
