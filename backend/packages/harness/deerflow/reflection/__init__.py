"""反射（Reflection）模块 —— 动态加载与解析工具。

本模块为 DeerFlow 系统提供运行时动态导入能力，核心职责是将字符串形式的
模块路径（如 ``"langchain_openai:ChatOpenAI"``）解析为实际的 Python 对象
（类、变量等）。该能力被广泛用于：

1. **配置驱动实例化** —— 从 YAML/JSON 配置文件中读取模型名称，
   在运行时动态加载对应的 LangChain 集成类，无需在代码中硬编码 import。
2. **插件式架构** —— 第三方集成包可以按需安装、按路径引用，
   系统自动发现并加载，无需修改核心代码。
3. **类型安全校验** —— 解析时可传入 ``expected_type`` 或 ``base_class``
   进行类型/继承关系验证，确保加载的对象符合接口契约。

模块导出：
    - :func:`resolve_variable` —— 根据路径字符串解析变量或对象
    - :func:`resolve_class` —— 根据路径字符串解析类，并可校验基类

设计要点：
    - 使用 ``importlib.import_module`` 实现延迟导入，避免循环依赖。
    - 当目标模块不存在时，自动生成包含 ``uv add`` / ``pip install``
      安装提示的异常消息，降低用户排错成本。
    - 模块根名称到 PyPI 包名的映射表维护在
    ``MODULE_TO_PACKAGE_HINTS`` 中，覆盖主流 LangChain 集成。

典型用法::

    from deerflow.reflection import resolve_class

    # 从配置读取类路径，动态加载并校验基类
    LLMClass = resolve_class("langchain_openai:ChatOpenAI", base_class=BaseChatModel)
    llm = LLMClass(model="gpt-4o")
"""

from .resolvers import resolve_class, resolve_variable

__all__ = ["resolve_class", "resolve_variable"]
