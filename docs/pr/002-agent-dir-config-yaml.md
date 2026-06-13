# 002 — fix(agents): require config.yaml in resolve_agent_dir to skip memory-only directories

| 字段 | 值 |
|------|-----|
| PR | #3481 |
| Commit | `b3c2cc42` |
| 状态 | Merged |
| 合并者 | WillemJiang |
| 合并时间 | 2026-06-10 |
| 分支 | → upstream/main |
| 改动规模 | 2 文件 +77/-2 行 |

## 问题

Issue #3390：当启用 memory 功能后，与一个 legacy 共享 agent 进行第一次对话时，
系统会为该 agent 创建一个 per-user 目录，其中只包含 `memory.json`（没有 `config.yaml`）。
在第二次对话时，`resolve_agent_dir()` 返回了这个不完整的目录，导致
`load_agent_config()` 报错 "Agent config not found"。

用户现象：第一次对话正常，第二次对话直接失败。

## 根因

`resolve_agent_dir()` 的查找逻辑（`agents_config.py`）：

```
1. 检查 per-user 目录：{base_dir}/users/{user_id}/agents/{agent_name}/
2. 如果存在 → 直接返回
3. 否则检查 legacy 目录：{base_dir}/agents/{agent_name}/
```

问题在第 2 步：MemoryMiddleware 在 per-user 目录下创建了 `memory.json` 后，
该目录就"存在"了。但 `resolve_agent_dir()` 只检查目录是否存在，不检查
目录是否包含完整的 agent 定义（`config.yaml`）。

与此同时，`list_custom_agents()` 已经有 `config.yaml` 存在性检查，
两个函数的行为不一致。

## 方案

在 `resolve_agent_dir()` 的两个路径（per-user 和 legacy）中都增加
`config.yaml` 存在性检查：

```python
# 修改前
if per_user_dir.exists():
    return per_user_dir

# 修改后
config_yaml = per_user_dir / "config.yaml"
if per_user_dir.exists() and config_yaml.is_file():
    return per_user_dir
```

这样，只包含 `memory.json` 的目录会被跳过，继续查找 legacy 目录或返回 None。
与 `list_custom_agents()` 的行为保持一致。

## 取舍

| 选择 | 理由 |
|------|------|
| 在 `resolve_agent_dir` 加 config.yaml 检查 | 最小改动，直接修复根因 |
| 不修改 MemoryMiddleware 的写入逻辑 | Memory 的写入行为是正确的，不应为查找逻辑的缺陷修改写入端 |

**放弃的方案**：
- 删除 memory-only 目录 — 会丢失用户的 memory 数据
- 修改 MemoryMiddleware 避免创建目录 — 写入逻辑嵌套深，影响面大
- 在 `load_agent_config` 中跳过错误 — 掩盖问题，可能引发下游错误

## 验证

- 新增回归测试：覆盖 per-user 目录只有 memory.json → 正确 fallback 到 legacy 目录
- 已有测试全部通过
- `ruff check` + `ruff format --check` 通过
