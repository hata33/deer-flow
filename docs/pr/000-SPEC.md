# PR 详情文档 — 规格要求说明书

## 目的

记录 hata 向 DeerFlow upstream 贡献的每个已合并 PR 的完整技术细节，包括：
- 做了什么（What）
- 为什么这么做（Why）
- 解决了什么问题（Problem）
- 有哪些取舍（Trade-offs）

## 文件命名

```
NNN-<short-slug>.md
```

- `NNN`: 三位序号，从 `001` 开始
- `<short-slug>`: 小写英文短标识，用 `-` 连接

示例：`001-strip-base64-history.md`

## 每个文件的必含章节

```markdown
# NNN — <PR 标题>

| 字段 | 值 |
|------|-----|
| PR | #<编号> |
| Commit | <短 hash> |
| 状态 | Merged |
| 合并者 | <maintainer> |
| 分支 | fix/xxx → upstream/main |

## 问题

<!-- Issue 描述，用户视角的痛点 -->

## 根因

<!-- 技术层面的因果链，精确到文件和行号 -->

## 方案

<!-- 做了什么改动，调用方视角 -->

## 取舍

<!-- 为什么选这个方案而非其他方案，放弃了什么 -->

## 验证

<!-- 跑了什么测试，结果如何 -->
```

## 已收录 PR 索引

| 编号 | PR | 标题 | 文件 |
|------|-----|------|------|
| 001 | #3461 | feat(models): add StepFun reasoning model adapter | 001-stepfun-model-adapter.md |
| 002 | #3481 | fix(agents): require config.yaml in resolve_agent_dir | 002-agent-dir-config-yaml.md |
| 003 | #3514 | fix(channels): reload config on channel restart | 003-channel-restart-reload.md |
| 004 | #3535 | fix(history): strip base64 image data from REST endpoints | 004-strip-base64-history.md |
