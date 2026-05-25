# User — 用户信息持久化

## 模块路径

`deerflow.persistence.user`

## 文件结构

```
user/
├── __init__.py    # 导出 UserRow
└── model.py       # UserRow ORM 模型（users 表定义）
```

## 解决的问题

DeerFlow 需要存储用户账户信息以支持认证和授权。用户信息是所有权体系的基础——所有持久化实体的 `user_id` 字段都指向 `users` 表。

## 为什么只有 Model 没有 Repository

`UserRow` 的 ORM 模型定义在 harness 层，但 Repository 实现位于应用层 `app.gateway.auth.repositories.sqlite`。

原因：
- Repository 需要在 ORM 行和 auth 模块的 Pydantic `User` 类之间转换
- auth 模块的 Pydantic 模型定义在 app 层
- harness 层不能导入 app 层代码（依赖方向约束，由 `tests/test_harness_boundary.py` 在 CI 中强制执行）

将 ORM 模型放在 harness 持久化包中的原因：
- 确保 `Base.metadata.create_all()` 能发现并创建 `users` 表
- 与其他表共享同一个引擎和连接池
- 统一的表初始化代码路径

## 数据模型 — UserRow

### 表名: `users`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | String(36), PK | UUID 字符串（36 字符，兼容 SQLite 和 PostgreSQL） |
| `email` | String(320), UNIQUE, INDEX | 用户邮箱（RFC 5321 最大长度） |
| `password_hash` | String(128), nullable | 密码哈希（OAuth 用户无密码） |
| `system_role` | String(16) | 系统角色（"admin" / "user"） |
| `created_at` | DateTime(tz) | 创建时间 |
| `oauth_provider` | String(32), nullable | OAuth 提供商名称（如 "google"、"github"） |
| `oauth_id` | String(128), nullable | OAuth 提供商中的用户 ID |
| `needs_setup` | Boolean | 是否需要初始设置 |
| `token_version` | int | Token 版本号（用于强制登出） |

### 索引

| 索引名 | 列 | 类型 | 条件 | 用途 |
|--------|-----|------|------|------|
| (email unique) | `email` | UNIQUE | 无 | 登录查找 |
| `idx_users_oauth_identity` | `(oauth_provider, oauth_id)` | UNIQUE (partial) | `oauth_provider IS NOT NULL AND oauth_id IS NOT NULL` | OAuth 账户唯一性 |

### 部分唯一索引设计

OAuth 字段使用**部分唯一索引**（Partial Unique Index）：
- 只约束 `oauth_provider` 和 `oauth_id` **都非 NULL** 的行
- NULL/NULL 的行（纯密码账户）不受约束
- 允许密码账户和 OAuth 账户共存

SQLite 使用 `sqlite_where=text("...")` 实现部分索引。

### UUID 字符串主键

`id` 使用 36 字符的 UUID 字符串而非数据库原生 UUID 类型，因为：
- SQLite 不原生支持 UUID 类型
- 跨后端兼容（同一套代码在 SQLite 和 PostgreSQL 上都能运行）

### `system_role` 使用字符串而非枚举

避免新增角色时需要执行 `ALTER TABLE`。新增角色只需在应用层识别新的字符串值即可。

### `token_version` — 强制登出机制

递增 `token_version` 可使所有已发行的 JWT 失效。JWT 验证时比对 Token 中的版本号与数据库中的版本号，不匹配则拒绝。

### `needs_setup` — 初始设置标志

新注册用户可能需要完成初始设置流程（如设置密码、选择偏好），此标志用于引导用户完成设置。
