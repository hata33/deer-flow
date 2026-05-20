"""SQLAlchemy 方言感知的 JSON 值匹配（支持 SQLite 和 PostgreSQL）。

本模块实现了跨数据库方言的 JSON 字段查询谓词。
由于 SQLite 和 PostgreSQL 处理 JSON 数据的语法不同：
  - SQLite 使用 json_type() / json_extract() 函数
  - PostgreSQL 使用 json_typeof() / ->> 运算符

JsonMatch 类封装了这些差异，使上层代码无需关心底层方言。

核心功能:
  - JsonMatch: SQLAlchemy 自定义表达式元素，实现 column[key] == value 语义
  - validate_metadata_filter_key:   验证 JSON 过滤键是否安全
  - validate_metadata_filter_value: 验证 JSON 过滤值是否为允许的类型

为什么需要这个模块:
  threads_meta 表的 metadata_json 列是 JSON 类型，需要支持按内部键值对过滤查询。
  但不同数据库的 JSON 查询语法差异很大，直接写 SQL 无法跨数据库兼容。
  JsonMatch 通过 SQLAlchemy 的编译扩展机制，为每种方言生成对应的 SQL。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import BigInteger, Float, String, bindparam
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.expression import ColumnElement
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import Boolean, TypeEngine

# 键名字符集正则：只允许字母、数字、下划线和连字符
# 为什么限制字符集：键会被直接插入到编译后的 SQL 路径表达式中，
# 如果允许任意字符，可能造成 SQL/JSONPath 注入
_KEY_CHARSET_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# 允许的元数据过滤值类型
# 限制类型的原因为：
# 1. 列表/字典/字节等类型无法安全地编译为 SQL 谓词
# 2. 静默转换（如 str()）会产生错误的匹配结果
# 3. 不可哈希类型会破坏 SQLAlchemy 的 inherit_cache 不变量
ALLOWED_FILTER_VALUE_TYPES: tuple[type, ...] = (type(None), bool, int, float, str)

# SQLite 在绑定超出有符号 64 位范围的值时会溢出；
# PostgreSQL 在 BIGINT 类型转换时也会溢出。
# 在验证阶段就拒绝超范围值，避免运行时数据库错误。
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def validate_metadata_filter_key(key: object) -> bool:
    """验证键是否安全可用作 JSON 元数据过滤键。

    安全的键必须是匹配 [A-Za-z0-9_-]+ 模式的字符串。
    限制字符集是因为键会被插入到编译后的 SQL 路径表达式中
    （如 $."<key>" 或 -> 文字），过于宽松的模式会打开注入攻击面。
    """
    return isinstance(key, str) and bool(_KEY_CHARSET_RE.match(key))


def validate_metadata_filter_value(value: object) -> bool:
    """验证值是否为 JSON 元数据过滤允许的类型。

    允许的类型: None, bool, int（有符号 64 位范围内）, float, str

    整数值额外限制在有符号 64 位范围 [-2**63, 2**63-1] 内，
    因为 SQLite 绑定超范围值会溢出，PostgreSQL 在 BIGINT 转换时也会溢出。
    """
    if not isinstance(value, ALLOWED_FILTER_VALUE_TYPES):
        return False
    # isinstance(True, int) 为 True，所以需要先检查 bool 再检查 int
    if isinstance(value, int) and not isinstance(value, bool):
        if not (_INT64_MIN <= value <= _INT64_MAX):
            return False
    return True


class JsonMatch(ColumnElement):
    """方言可移植的 JSON 列键值匹配表达式: column[key] == value。

    编译结果:
      - SQLite:  json_type(json_col, '$."key"') 和 json_extract() 组合
      - PostgreSQL: json_typeof(json_col -> 'key') 和 ->> 运算符组合

    实现类型安全的比较，区分 bool 与 int、NULL 与键不存在。

    限制:
      - key 必须是匹配 [A-Za-z0-9_-]+ 的单层字符串键
      - value 必须是 None, bool, int（有符号 64 位）, float, str 之一

    为什么继承 ColumnElement:
      这是 SQLAlchemy 自定义 SQL 表达式的标准方式，
      通过 @compiles 装饰器为不同方言注册编译函数。
    """

    inherit_cache = True  # 启用缓存，提升重复编译的性能
    type = Boolean()      # 表达式返回布尔类型
    _is_implicitly_boolean = True  # 告诉 SQLAlchemy 这是一个布尔表达式

    # 定义遍历内部属性，用于 SQLAlchemy 的克隆/遍历机制
    _traverse_internals = [
        ("column", InternalTraversal.dp_clauseelement),
        ("key", InternalTraversal.dp_string),
        ("value", InternalTraversal.dp_plain_obj),
    ]

    def __init__(self, column: ColumnElement, key: str, value: object) -> None:
        # 在构造时就验证 key 和 value 的安全性
        if not validate_metadata_filter_key(key):
            raise ValueError(f"JsonMatch key must match {_KEY_CHARSET_RE.pattern!r}; got: {key!r}")
        if not validate_metadata_filter_value(value):
            if isinstance(value, int) and not isinstance(value, bool):
                raise TypeError(f"JsonMatch int value out of signed 64-bit range [-2**63, 2**63-1]: {value!r}")
            raise TypeError(f"JsonMatch value must be None, bool, int, float, or str; got: {type(value).__name__!r}")
        self.column = column  # 要查询的 JSON 列
        self.key = key        # JSON 对象中的键名
        self.value = value    # 要匹配的值
        super().__init__()


@dataclass(frozen=True)
class _Dialect:
    """每种数据库方言在编译 JSON 类型/值比较时使用的配置。

    不同数据库的 JSON 类型名称和转换方式不同，
    这个数据类将差异封装为配置，使 _build_clause 函数保持通用。
    """
    null_type: str           # JSON null 类型的字符串表示
    num_types: tuple[str, ...]  # 数值类型的名称元组
    num_cast: str            # 数值转换的目标 SQL 类型
    int_types: tuple[str, ...]  # 整数类型的名称元组
    int_cast: str            # 整数转换的目标 SQL 类型
    int_guard: str | None    # 整数防护正则（仅 PostgreSQL 需要）
    string_type: str         # 字符串类型的名称
    bool_type: str | None    # 布尔类型的名称（SQLite 为 None）


# SQLite 方言配置
# SQLite 的 json_type() 直接返回 'integer'/'real'/'text'/'null' 等具体类型名
_SQLITE = _Dialect(
    null_type="null",
    num_types=("integer", "real"),
    num_cast="REAL",
    int_types=("integer",),
    int_cast="INTEGER",
    int_guard=None,           # SQLite 不需要整数防护，json_type 已区分 integer/real
    string_type="text",
    bool_type=None,           # SQLite 没有独立的 boolean 类型，直接比较 'true'/'false'
)

# PostgreSQL 方言配置
# PostgreSQL 的 json_typeof() 返回 'number'/'string'/'boolean'/'null' 等
# 'number' 同时包含整数和浮点数，因此需要额外的正则防护来区分
_PG = _Dialect(
    null_type="null",
    num_types=("number",),
    num_cast="DOUBLE PRECISION",
    int_types=("number",),
    int_cast="BIGINT",
    int_guard="'^-?[0-9]+$'",  # PostgreSQL 专用：用正则区分整数和浮点数
    string_type="string",
    bool_type="boolean",
)


def _bind(compiler: SQLCompiler, value: object, sa_type: TypeEngine[Any], **kw: Any) -> str:
    """将 Python 值绑定为 SQL 参数。

    使用 SQLAlchemy 的 bindparam 机制，确保值通过参数化查询传递，
    而不是直接拼接到 SQL 字符串中，防止 SQL 注入。
    """
    param = bindparam(None, value, type_=sa_type)
    return compiler.process(param, **kw)


def _type_check(typeof: str, types: tuple[str, ...]) -> str:
    """生成 JSON 类型检查的 SQL 片段。

    单类型: typeof = 'integer'
    多类型: typeof IN ('integer', 'real')
    """
    if len(types) == 1:
        return f"{typeof} = '{types[0]}'"
    quoted = ", ".join(f"'{t}'" for t in types)
    return f"{typeof} IN ({quoted})"


def _build_clause(compiler: SQLCompiler, typeof: str, extract: str, value: object, dialect: _Dialect, **kw: Any) -> str:
    """根据值的类型构建方言可移植的比较子句。

    核心逻辑：先通过 json_typeof/json_type 检查 JSON 值的类型，
    再通过类型转换进行比较。这样可以在 JSON 列中实现类型安全的匹配，
    区分 bool 与 int、NULL 与键不存在。

    处理顺序很重要:
      1. None:      检查 JSON 类型是否为 null
      2. bool:      必须在 int 之前检查（Python 中 bool 是 int 的子类）
      3. int:       类型检查 + 类型转换 + 值比较
      4. float:     类型检查 + 类型转换 + 值比较
      5. str:       类型检查 + 直接比较
    """
    # NULL 值：直接比较 JSON 类型是否为 'null'
    if value is None:
        return f"{typeof} = '{dialect.null_type}'"

    # 布尔值：必须在整数之前检查，因为 Python 中 bool 是 int 的子类
    if isinstance(value, bool):
        bool_str = "true" if value else "false"
        if dialect.bool_type is None:
            # SQLite 没有独立的 boolean 类型，直接比较字符串
            return f"{typeof} = '{bool_str}'"
        # PostgreSQL 有 boolean 类型，需要同时检查类型和值
        return f"({typeof} = '{dialect.bool_type}' AND {extract} = '{bool_str}')"

    # 整数值：类型检查 + 类型转换后比较
    if isinstance(value, int):
        bp = _bind(compiler, value, BigInteger(), **kw)
        if dialect.int_guard:
            # PostgreSQL 专用：使用 CASE 表达式防止将浮点数错误转换为整数
            # 当 json_typeof = 'number' 时，值可能是 1.5（浮点数），
            # 直接 CAST 为 BIGINT 会报错。正则 '^-?[0-9]+$' 只匹配纯整数。
            return f"(CASE WHEN {_type_check(typeof, dialect.int_types)} AND {extract} ~ {dialect.int_guard} THEN CAST({extract} AS {dialect.int_cast}) END = {bp})"
        # SQLite：json_type 已区分 integer 和 real，无需额外防护
        return f"({_type_check(typeof, dialect.int_types)} AND CAST({extract} AS {dialect.int_cast}) = {bp})"

    # 浮点数值：类型检查 + REAL/DOUBLE PRECISION 转换后比较
    if isinstance(value, float):
        bp = _bind(compiler, value, Float(), **kw)
        return f"({_type_check(typeof, dialect.num_types)} AND CAST({extract} AS {dialect.num_cast}) = {bp})"

    # 字符串值：类型检查 + 直接比较
    bp = _bind(compiler, str(value), String(), **kw)
    return f"({typeof} = '{dialect.string_type}' AND {extract} = {bp})"


@compiles(JsonMatch, "sqlite")
def _compile_sqlite(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    """为 SQLite 方言编译 JsonMatch 表达式。

    生成使用 SQLite json 函数的 SQL:
      - json_type(col, '$."key"')  获取 JSON 值的类型
      - json_extract(col, '$."key"')  提取 JSON 值

    SQLite 使用 JSONPath 语法（$."key"）定位 JSON 对象中的字段。
    """
    if not validate_metadata_filter_key(element.key):
        raise ValueError(f"Key escaped validation: {element.key!r}")
    col = compiler.process(element.column, **kw)
    path = f'$."{element.key}"'
    typeof = f"json_type({col}, '{path}')"     # 获取 JSON 值类型
    extract = f"json_extract({col}, '{path}')"  # 提取 JSON 值
    return _build_clause(compiler, typeof, extract, element.value, _SQLITE, **kw)


@compiles(JsonMatch, "postgresql")
def _compile_pg(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    """为 PostgreSQL 方言编译 JsonMatch 表达式。

    生成使用 PostgreSQL JSON 运算符的 SQL:
      - json_typeof(col -> 'key')  获取 JSON 值类型
      - col ->> 'key'              提取 JSON 值为文本

    PostgreSQL 使用 ->> 运算符提取 JSON 值，比 SQLite 的 json_extract 更简洁。
    """
    if not validate_metadata_filter_key(element.key):
        raise ValueError(f"Key escaped validation: {element.key!r}")
    col = compiler.process(element.column, **kw)
    typeof = f"json_typeof({col} -> '{element.key}')"  # 获取 JSON 值类型
    extract = f"({col} ->> '{element.key}')"             # 提取 JSON 值
    return _build_clause(compiler, typeof, extract, element.value, _PG, **kw)


@compiles(JsonMatch)
def _compile_default(element: JsonMatch, compiler: SQLCompiler, **kw: Any) -> str:
    """不支持的方言：抛出异常。

    JsonMatch 只支持 sqlite 和 postgresql 两种方言。
    如果使用了其他数据库（如 MySQL），会在此处明确报错。
    """
    raise NotImplementedError(f"JsonMatch supports only sqlite and postgresql; got dialect: {compiler.dialect.name}")


def json_match(column: ColumnElement, key: str, value: object) -> JsonMatch:
    """创建 JSON 键值匹配表达式的便捷工厂函数。

    用法示例:
        json_match(ThreadMetaRow.metadata_json, "status", "active")
        生成等价于 SQL: metadata_json->>'status' = 'active' 的表达式

    作用：封装 JsonMatch 的构造细节，提供更简洁的调用接口。
    """
    return JsonMatch(column, key, value)
