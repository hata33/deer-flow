# 08 - 配置热加载边界：哪些字段需要重启生效

> 本文档分析 `config/reload_boundary.py`（issue #3144），解答"改了 config.yaml 后哪些立即生效、哪些必须重启"。

---

## 一、问题背景

网关的请求依赖通过 `get_app_config()` 在**每个请求**时解析 `AppConfig`，所以大部分配置字段在下一条消息时自动生效。但部分基础设施字段在**启动时捕获一次**，运行期间不会重建——修改它们需要重启网关进程。

之前没有一个明确的地方记录"哪些字段需要重启"，开发者/运维只能靠读代码猜测。

---

## 二、解决方案：集中注册表

`reload_boundary.py` 定义了 `STARTUP_ONLY_FIELDS` 注册表——所有需要重启的字段及其原因：

```python
STARTUP_ONLY_FIELDS: dict[str, str] = {
    "database":      "init_engine_from_config() runs once during startup; SQLAlchemy engine holds connection pool...",
    "checkpointer":  "make_checkpointer() binds the persistent checkpointer once at startup...",
    "run_events":    "make_run_event_store() picks memory- vs SQL-backed implementation at startup...",
    "stream_bridge": "make_stream_bridge() constructs the stream-bridge singleton once...",
    "sandbox":       "get_sandbox_provider() caches the provider singleton...",
    "log_level":     "apply_logging_level() runs only during app.py startup...",
    "channels":      "start_channel_service() is invoked once during startup; IM clients not rebuilt...",
}
```

每个值不只是说"需要重启"，而是解释**哪段代码在启动时捕获了快照**——运维人员知道需要重启哪个子系统。

---

## 三、双向漂移测试

注册表与 `AppConfig` Pydantic schema 通过 `STARTUP_ONLY_PREFIX` 标记保持一致：

| 方向 | 保证 |
|------|------|
| 注册表 → schema | 每个注册字段在 `Field(description=...)` 中带 `"startup-only:"` 前缀 |
| schema → 注册表 | 任何字段用了 `"startup-only:"` 前缀必须在注册表中 |

测试 `test_reload_boundary` 强制双向一致——漏注册或漏标记都会失败。

---

## 四、热加载 vs 需重启分类

```
立即生效（改 config.yaml 后下一条消息生效）
├── models          → 模型列表、参数
├── agents          → agent 配置（system_prompt、max_turns）
├── tool_search     → 延迟工具搜索开关
├── memory          → 记忆系统配置
├── guardrails      → 护栏规则
└── safety          → 安全过滤配置

需要重启（改 config.yaml 后必须重启网关进程）
├── database        → SQLAlchemy 引擎 + 连接池
├── checkpointer    → 持久化检查点后端
├── run_events      → 运行事件存储（内存 vs SQL）
├── stream_bridge   → 流式桥接单例
├── sandbox         → 沙箱提供者单例
├── log_level       → 日志级别
└── channels        → IM 渠道客户端
```

---

## 五、设计决策

### 为什么不自动检测

理论上可以扫描所有 `get_app_config()` 的调用点，判断是否在请求路径内。但这需要全程序分析，且无法处理"启动时缓存单例"的模式。集中注册表是显式、可审计的。

### 为什么是 per-section 而非 per-leaf

注册表只记录顶层字段（`"database"` 而非 `"database.url"`），因为：
- 重启粒度是整个子系统，不是单个配置项
- 子系统内部字段的"需要重启"判断一致，无需细化

### 为什么 channels 不在 AppConfig schema 中

IM 渠道凭据直接由 `start_channel_service()` 消费，不走 Pydantic 验证。注册表是它唯一的规范记录位置。

---

## 六、使用方式

```python
from deerflow.config.reload_boundary import is_startup_only_field, format_field_description

# 检查字段是否需要重启
is_startup_only_field("database")  # True
is_startup_only_field("models")    # False

# 生成带标记的 Field description
format_field_description("sandbox", field_doc="Use 'local' for Docker or 'remote' for provisioner.")
# → "startup-only: get_sandbox_provider() caches...\n\nUse 'local' for Docker..."
```
