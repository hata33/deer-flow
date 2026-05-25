# Feedback — 用户反馈持久化

## 模块路径

`deerflow.persistence.feedback`

## 文件结构

```
feedback/
├── __init__.py    # 导出 FeedbackRow, FeedbackRepository
├── model.py       # FeedbackRow ORM 模型（feedback 表定义）
└── sql.py         # FeedbackRepository — 反馈数据仓库
```

## 解决的问题

用户对 Agent 运行结果的评价数据需要持久化，用于：

- 评估和改进 Agent 表现
- 按运行/线程/模型聚合反馈统计
- 支持反馈的创建、修改、删除和 upsert 操作
- 针对特定消息而非整个运行给出反馈

## 数据模型 — FeedbackRow

### 表名: `feedback`

| 字段 | 类型 | 说明 |
|------|------|------|
| `feedback_id` | String(64), PK | 反馈唯一标识（UUID） |
| `run_id` | String(64), INDEX | 关联的运行 ID |
| `thread_id` | String(64), INDEX | 关联的线程 ID |
| `user_id` | String(64), INDEX, nullable | 反馈提交者（可为空表示匿名） |
| `message_id` | String(64), nullable | 指向 RunEventStore 中的特定事件 |
| `rating` | int, NOT NULL | +1（点赞）或 -1（点踩） |
| `comment` | Text, nullable | 可选文字反馈 |
| `created_at` | DateTime(tz) | 创建时间 |

### 唯一约束

`(thread_id, run_id, user_id)` — 确保每个用户对同一线程中的同一运行只有一条反馈。这是 upsert 语义的基础。

### `message_id` 可选设计

`message_id` 允许反馈指向运行中的**特定消息**而非整个运行。这使细粒度反馈成为可能（如"这条 AI 回答不好"而非"这次运行不好"）。

### `user_id` 可空设计

可为空以支持匿名反馈场景。

## Repository — FeedbackRepository

### 核心方法

#### `create()` — 创建反馈
```
1. 校验 rating（必须为 +1 或 -1）
2. 解析 user_id（三态语义）
3. 生成 UUID 作为 feedback_id
4. 写入数据库
5. 刷新获取数据库默认值
6. 返回字典格式
```

#### `upsert()` — 创建或更新反馈
```
1. 校验 rating
2. 解析 user_id
3. 查找 (thread_id, run_id, user_id) 组合
   ├── 已存在 → 更新 rating, comment, 重置 created_at
   └── 不存在 → 创建新记录
4. 返回最终反馈数据
```
这是用户修改反馈的核心方法。用户无需先删后建，直接调用即可。

#### `get()` — 获取单条反馈
包含所有者过滤：非 None 时只返回属于该用户的反馈。

#### `list_by_run()` — 列出运行的反馈
按创建时间升序（最早的在前），支持所有者过滤和分页。

#### `list_by_thread()` — 列出线程的反馈
同上，范围扩大到整个线程。

#### `list_by_thread_grouped()` — 按运行分组返回
返回 `{run_id: feedback_dict, ...}` 格式，用于一次性获取线程中所有运行的反馈状态，避免逐个查询。

#### `delete()` — 删除单条反馈
包含所有者检查，防止用户删除他人反馈。返回 `True`/`False`。

#### `delete_by_run()` — 删除用户对某运行的反馈
根据 `(thread_id, run_id, user_id)` 定位并删除。

#### `aggregate_by_run()` — 聚合统计
使用数据库端 SQL 聚合，避免加载所有记录到应用层：

```sql
SELECT COUNT(*) as total,
       COALESCE(SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) as positive,
       COALESCE(SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0) as negative
FROM feedback
WHERE thread_id = ? AND run_id = ?
```

返回：`{run_id, total, positive, negative}`

### 三态 user_id 语义

| 值 | 行为 | 场景 |
|----|------|------|
| `AUTO`（默认） | 从 contextvar 解析 | 正常请求 |
| 显式 `str` | 使用提供的值 | 测试、跨用户操作 |
| 显式 `None` | 绕过所有权过滤 | 迁移、管理操作 |

### _row_to_dict() — 格式转换

将 ORM 行转为字典，datetime 转 ISO 字符串，确保与 JSON 序列化兼容。
