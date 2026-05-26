# NL2SQL Agent 方案

**目标**: 将 DeerFlow Agent 改造为自然语言转 SQL 的数据分析助手，用户用中文提问，Agent 自动理解 Schema、生成 SQL、执行查询、可视化结果。

---

## 1. 架构总览

```
用户: "上个月销售额最高的 5 个地区是哪些？"
    │
    ▼ Schema 感知层
    ├── Schema 中间件注入表结构上下文
    └── Schema 缓存（避免每次查数据库）
    │
    ▼ 推理层（Agent ReAct 循环）
    ├── Thought: 分析用户意图，确定需要哪些表
    ├── Action: 调用 NL2SQL 工具集
    └── Observation: 获取结果，决定下一步
    │
    ▼ 安全层
    ├── SQL 语法校验
    ├── 只读保护（禁止 INSERT/UPDATE/DELETE）
    ├── 行数限制（max_rows）
    └── 超时控制
    │
    ▼ 执行层
    ├── 执行 SQL → 获取结果
    ├── 结果格式化（表格/图表描述）
    └── 生成自然语言解释
```

---

## 2. 需要新建的文件

```
backend/packages/harness/deerflow/
    ├── community/nl2sql/                  # NL2SQL 工具集
    │   ├── __init__.py
    │   ├── tools.py                       # 核心工具定义
    │   ├── schema_cache.py                # Schema 缓存
    │   ├── sql_validator.py               # SQL 安全校验
    │   └── result_formatter.py            # 结果格式化
    │
    ├── agents/middlewares/
    │   └── nl2sql_middleware.py           # Schema 上下文注入
    │
    └── config/
        └── nl2sql_config.py              # 配置模型

skills/
    └── custom/nl2sql/                     # NL2SQL 技能
        └── SKILL.md                       # 技能定义

docs/core/场景/NL2SQL/                     # 文档（本目录）
```

---

## 3. 核心工具设计

### 3.1 工具清单

| 工具 | 功能 | 输入 | 输出 |
|------|------|------|------|
| `list_tables` | 列出所有可用表 | 无 | 表名列表 + 注释 |
| `describe_table` | 查看表结构 | 表名 | 列名、类型、注释、外键 |
| `read_schema` | 获取完整 Schema | 无 | DDL 语句 + 关系图 |
| `sql_query` | 执行 SQL 查询 | SQL 语句 | 结果集（行+列） |
| `explain_query` | 查看执行计划 | SQL 语句 | EXPLAIN 输出 |
| `sample_data` | 查看表样本数据 | 表名、行数 | 前 N 行数据 |

### 3.2 工具实现骨架

```python
# community/nl2sql/tools.py

from langchain_core.tools import tool
from .sql_validator import validate_sql
from .schema_cache import SchemaCache
from .result_formatter import format_result

_schema_cache: SchemaCache | None = None


def _get_cache(config) -> SchemaCache:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = SchemaCache(
            database_url=config["database_url"],
            ttl=config.get("schema_cache_ttl", 3600),
        )
    return _schema_cache


@tool(parse_docstring=True)
def list_tables() -> str:
    """列出数据库中所有可用表名和注释。

    Returns:
        表名和注释的列表
    """
    # SHOW TABLES / information_schema 查询


@tool(parse_docstring=True)
def describe_table(table_name: str) -> str:
    """查看指定表的结构，包括列名、类型、注释和外键关系。

    Args:
        table_name: 要查看的表名

    Returns:
        表结构的详细描述
    """
    # DESCRIBE / information_schema.COLUMNS


@tool(parse_docstring=True)
def sql_query(query: str, max_rows: int = 1000) -> str:
    """执行 SQL 查询并返回结果。仅支持 SELECT 查询。

    Args:
        query: 要执行的 SQL 查询语句
        max_rows: 最大返回行数，默认 1000

    Returns:
        查询结果的格式化文本
    """
    # 1. 安全校验
    error = validate_sql(query)
    if error:
        return f"SQL 校验失败: {error}"

    # 2. 注入行数限制
    if "LIMIT" not in query.upper():
        query = f"{query.rstrip(';')} LIMIT {max_rows}"

    # 3. 执行
    engine = create_engine(database_url)
    with engine.connect() as conn:
        result = conn.execute(text(query), timeout=30)
        rows = result.fetchmany(max_rows)
        columns = list(result.keys())

    # 4. 格式化
    return format_result(columns, rows)
```

---

## 4. SQL 安全校验器

```python
# community/nl2sql/sql_validator.py

import re
from typing import Optional

# 禁止的 SQL 关键词
BLOCKED_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "REPLACE", "GRANT", "REVOKE", "EXEC", "EXECUTE",
    "INTO OUTFILE", "INTO DUMPFILE", "LOAD DATA",
}

# 危险函数
BLOCKED_FUNCTIONS = {
    "SLEEP", "BENCHMARK", "LOAD_FILE", "INTO",
}


def validate_sql(query: str) -> Optional[str]:
    """校验 SQL 是否安全。返回 None 表示通过，否则返回错误原因。"""

    # 1. 去除注释
    cleaned = re.sub(r"--.*$", "", query, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned_upper = cleaned.upper().strip()

    # 2. 必须以 SELECT 开头
    if not cleaned_upper.startswith("SELECT") and not cleaned_upper.startswith("WITH"):
        return "仅支持 SELECT 查询，不允许修改数据"

    # 3. 检查禁止关键词
    for keyword in BLOCKED_KEYWORDS:
        if keyword in cleaned_upper:
            return f"包含禁止操作: {keyword}"

    # 4. 检查危险函数
    for func in BLOCKED_FUNCTIONS:
        pattern = rf"\b{func}\s*\("
        if re.search(pattern, cleaned_upper):
            return f"包含禁止函数: {func}"

    # 5. 检查分号（防止多语句注入）
    if ";" in cleaned.rstrip(";"):
        return "不允许执行多条 SQL 语句"

    return None  # 通过
```

---

## 5. Schema 缓存

```python
# community/nl2sql/schema_cache.py

import json
import time
from dataclasses import dataclass
from sqlalchemy import create_engine, text, inspect


@dataclass
class SchemaInfo:
    tables: dict          # {table_name: {columns, comment, ...}}
    relationships: list   # [{from_table, from_col, to_table, to_col}]
    ddl: str              # CREATE TABLE 语句
    cached_at: float


class SchemaCache:
    def __init__(self, database_url: str, ttl: int = 3600):
        self._url = database_url
        self._ttl = ttl
        self._cache: SchemaInfo | None = None

    def get(self) -> SchemaInfo:
        if self._cache and (time.time() - self._cache.cached_at) < self._ttl:
            return self._cache
        self._cache = self._refresh()
        return self._cache

    def invalidate(self):
        self._cache = None

    def _refresh(self) -> SchemaInfo:
        engine = create_engine(self._url)
        inspector = inspect(engine)

        tables = {}
        for table_name in inspector.get_table_names():
            columns = inspector.get_columns(table_name)
            pk = inspector.get_pk_constraint(table_name)
            fks = inspector.get_foreign_keys(table_name)
            tables[table_name] = {
                "columns": columns,
                "primary_key": pk,
                "foreign_keys": fks,
            }

        relationships = []
        for tname, info in tables.items():
            for fk in info["foreign_keys"]:
                relationships.append({
                    "from_table": tname,
                    "from_col": fk["constrained_columns"],
                    "to_table": fk["referred_table"],
                    "to_col": fk["referred_columns"],
                })

        return SchemaInfo(
            tables=tables,
            relationships=relationships,
            ddl="",  # 可选: 生成 DDL
            cached_at=time.time(),
        )
```

---

## 6. Schema 注入中间件

```python
# agents/middlewares/nl2sql_middleware.py

from deerflow.agents.middlewares.base import AgentMiddleware
from deerflow.agents.thread_state import ThreadState


class NL2SQLMiddleware(AgentMiddleware[ThreadState]):
    """在 LLM 调用前注入数据库 Schema 上下文。"""

    state_schema = ThreadState

    def __init__(self, schema_cache, max_schema_tokens=4000):
        self._schema_cache = schema_cache
        self._max_tokens = max_schema_tokens

    def before_model(self, state: ThreadState, config):
        messages = state.get("messages", [])
        if not messages:
            return state

        # 获取 Schema
        schema = self._schema_cache.get()
        schema_text = self._format_schema(schema)

        # 截断到 token 预算
        if len(schema_text) > self._max_tokens * 4:  # 粗估
            schema_text = schema_text[:self._max_tokens * 4]

        # 注入到第一条 HumanMessage
        injection = f"\n<database_schema>\n{schema_text}\n</database_schema>"

        first_human_idx = None
        for i, msg in enumerate(messages):
            if hasattr(msg, "type") and msg.type == "human":
                first_human_idx = i
                break

        if first_human_idx is not None:
            messages[first_human_idx].content += injection

        return state

    def _format_schema(self, schema) -> str:
        lines = []
        for table_name, info in schema.tables.items():
            cols = ", ".join(
                f"{c['name']} ({c['type']})"
                for c in info["columns"]
            )
            lines.append(f"TABLE {table_name}: {cols}")

        if schema.relationships:
            lines.append("\n关系:")
            for rel in schema.relationships:
                lines.append(
                    f"  {rel['from_table']}.{rel['from_col']} → "
                    f"{rel['to_table']}.{rel['to_col']}"
                )

        return "\n".join(lines)
```

---

## 7. NL2SQL 技能定义

```markdown
---
name: nl2sql
description: "自然语言转 SQL 数据分析助手。用户用自然语言提问，Agent 自动生成 SQL 查询数据库。"
allowed-tools:
  - list_tables
  - describe_table
  - read_schema
  - sql_query
  - explain_query
  - sample_data
  - bash
  - read_file
  - ask_clarification
---

## NL2SQL 技能指令

你是一个专业的数据分析 SQL 助手。你的职责是将用户的自然语言问题转换为准确的 SQL 查询。

### 工作流程

1. **理解需求**
   - 分析用户问题的意图
   - 确定需要查询哪些表
   - 如果不确定表结构，先调用 `list_tables` 或 `describe_table`

2. **生成 SQL**
   - 根据表结构和用户需求编写 SQL
   - 必须使用标准的 SELECT 语句
   - 复杂查询时考虑使用 CTE (WITH 子句)
   - 始终包含 LIMIT 子句

3. **验证和执行**
   - 检查 SQL 是否引用了正确的表名和列名
   - 调用 `sql_query` 执行
   - 如果出错，分析错误信息并修正 SQL

4. **解释结果**
   - 用自然语言解释查询结果
   - 如果结果为空，分析原因并建议修改查询
   - 对于大结果集，提供关键统计摘要

### 安全规则

- 只能执行 SELECT 查询
- 不允许修改任何数据
- 每次查询最多返回 1000 行
- 不执行可能影响数据库性能的查询（如全表扫描大表）

### 澄清场景

遇到以下情况必须先向用户确认：
- 用户问题可以有多种 SQL 解释
- 需要查询的表或列不确定
- 查询可能返回大量数据
- 用户使用了模糊的业务术语
```

---

## 8. 配置

```yaml
# config.yaml 新增
nl2sql:
  enabled: true
  database_url: "${DATABASE_URL}"
  dialect: "postgresql"          # postgresql / mysql / sqlite
  schema_cache_ttl: 3600         # Schema 缓存 1 小时
  max_schema_tokens: 4000        # Schema 注入 token 预算
  safety:
    max_rows: 1000               # 单次查询最大行数
    query_timeout: 30            # 查询超时（秒）
    allowed_statements:          # 允许的语句类型
      - "SELECT"
      - "WITH"
```

---

## 9. Agent 推理流程

```
用户: "每个产品类别的月销售额趋势"
    │
    ▼ Thought 1: 分析意图
    "用户想看按产品类别分组的月度销售额。
     需要先确认数据库中有哪些相关的表。"
    │
    ▼ Action: list_tables()
    Observation: "orders, order_items, products, categories, ..."
    │
    ▼ Thought 2: 查看表结构
    "看起来需要 orders + order_items + products + categories 四个表。
     先看 orders 表的结构。"
    │
    ▼ Action: describe_table("orders")
    Observation: "id, user_id, total_amount, status, created_at, ..."
    │
    ▼ Thought 3: 生成 SQL
    "现在我有足够信息生成 SQL 了。用 CTE 按月聚合。"
    │
    ▼ Action: sql_query("""
        SELECT
            c.name AS category,
            DATE_TRUNC('month', o.created_at) AS month,
            SUM(oi.quantity * oi.price) AS revenue
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        JOIN products p ON oi.product_id = p.id
        JOIN categories c ON p.category_id = c.id
        WHERE o.status = 'completed'
          AND o.created_at >= NOW() - INTERVAL '12 months'
        GROUP BY c.name, DATE_TRUNC('month', o.created_at)
        ORDER BY c.name, month
        LIMIT 500
    """)
    Observation: 结果表格...
    │
    ▼ Thought 4: 解释结果
    "查询成功，返回了 12 个月的数据。给用户一个清晰的总结。"
    │
    ▼ 最终输出
    "查询完成。以下是每个产品类别的月度销售额趋势:

    | 类别 | 最高月销售额 | 最低月销售额 | 趋势 |
    |------|------------|------------|------|
    | 电子产品 | ¥1.2M (3月) | ¥800K (7月) | 上升 |
    | 服装 | ¥950K (11月) | ¥600K (2月) | 稳定 |
    ...

    整体来看，电子产品类别的销售额最高且呈上升趋势。"
```

---

## 10. 高级特性

### 10.1 查询纠错循环

```
sql_query("SELECT ... GROUP BY categorie_name")
    │
    ▼ Observation: ERROR: column "categorie_name" does not exist
    │
    ▼ Thought: "列名拼写错误，应该是 category_name"
    │
    ▼ Action: sql_query("SELECT ... GROUP BY category_name")
    │
    ▼ Observation: 成功
```

Agent 利用 ReAct 的自我纠错能力自动修复 SQL 错误，不需要额外的纠错模块。

### 10.2 Schema 感知的查询优化

```python
# 中间件在注入 Schema 时附加统计信息
"""
TABLE orders (50 万行):
  id (PK), user_id, total_amount, status, created_at
  索引: idx_status, idx_created_at
  建议: 按 status/created_at 过滤可走索引
"""
```

帮助 Agent 避免全表扫描。

### 10.3 多轮查询上下文

```
用户: "上个月的销售额是多少？"
Agent: 执行 SQL → "上个月销售额为 ¥5.2M"

用户: "按地区拆分呢？"
Agent: 基于上一轮的 SQL，修改 GROUP BY → 执行 → "华东: ¥1.8M, 华南: ¥1.5M..."
```

Agent 自然地利用对话上下文做增量查询。

---

## 11. 实施步骤

| 阶段 | 内容 | 预计工期 |
|------|------|---------|
| **Phase 1** | 工具开发（6 个工具 + 校验器） | 2-3 天 |
| **Phase 2** | Schema 缓存 + 中间件 | 1-2 天 |
| **Phase 3** | 技能 SKILL.md + 配置 | 0.5 天 |
| **Phase 4** | 测试（安全校验、查询纠错、性能） | 2 天 |
| **Phase 5** | 文档 + 示例 | 1 天 |

---

## 12. 依赖

```txt
# requirements-nl2sql.txt
sqlalchemy>=2.0
psycopg2-binary>=2.9   # PostgreSQL
# 或 pymysql>=1.1      # MySQL
# 或 sqlite3            # SQLite（Python 内置）
```

---

## 参考

- [SQL-of-Thought: Multi-agent NL2SQL](https://arxiv.org/html/2509.00581v2) — 多 Agent NL2SQL 框架
- [NL2SQL Handbook](https://github.com/hkustdial/nl2sql_handbook) — NL2SQL 技术手册
- [Oracle NL2SQL Agent](https://blogs.oracle.com/cloud-infrastructure/nl2sql-agent-mcp-powered-data-insights) — MCP 驱动的 NL2SQL
- [DeerFlow 扩展指南](../../guides/02-extension-guide.md) — 工具/中间件扩展方法
- [DeerFlow 工具系统](../../core/tools/00-overview.md) — 工具注册机制
