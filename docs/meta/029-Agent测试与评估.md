# Agent 测试与评估

**问题**: Agent 的输出是非确定性的——同样的输入可能得到不同结果。传统测试（断言固定输出）对 Agent 几乎无效，需要专门的测试和评估方法。

---

## 问题 1：为什么 Agent 测试这么难？

| 维度 | 传统软件 | Agent 系统 |
|------|---------|-----------|
| 输出确定性 | 固定 | 非确定 |
| 执行路径 | 可预测 | LLM 自主决策 |
| 错误类型 | 逻辑 bug | 质量问题（幻觉、遗漏、误解） |
| 测试方法 | 单元测试 + 断言 | 评估 + 打分 |
| 回归检测 | 二进制（通过/失败） | 梯度（好一点/差一点） |

核心矛盾：**你不能断言 Agent 必须输出固定文本**，但你需要知道它是否在"变好"。

---

## 问题 2：DeerFlow 的测试分几层？

```
第 1 层: 组件测试（确定性）
    │   测中间件、工具、格式化等有固定输入/输出的模块
    │
    ▼
第 2 层: Prompt 测试（半确定性）
    │   测系统提示的组成部分是否正确拼接
    │
    ▼
第 3 层: 集成测试（需要 LLM）
    │   端到端对话质量，需要评估指标
    │
    ▼
第 4 层: 生产监控（持续评估）
    实际用户反馈 + 自动化指标
```

---

## 问题 3：组件测试怎么写？

测试有确定性输入/输出的模块：

```python
# 测试: 记忆注入按置信度排序
def test_format_memory_sorts_facts_by_confidence():
    facts = [
        Fact(content="偏好A", confidence=0.7),
        Fact(content="偏好B", confidence=0.95),
        Fact(content="偏好C", confidence=0.8),
    ]
    result = format_memory_for_injection({"facts": facts})
    assert result.index("偏好B") < result.index("偏好C")

# 测试: Token 超出预算时截断
def test_injection_respects_token_budget():
    facts = [Fact(content=f"fact_{i}", confidence=0.9) for i in range(100)]
    result = format_memory_for_injection(
        {"facts": facts},
        max_tokens=500  # 远不够放 100 条
    )
    # 验证: 结果不超过 500 token
    assert count_tokens(result) <= 500

# 测试: 修正格式包含 sourceError
def test_correction_fact_format():
    fact = Fact(
        content="用户喜欢 Python (avoid: 说用户喜欢 Java)",
        confidence=0.9,
        category="correction"
    )
    result = format_fact(fact)
    assert "(avoid:" in result
```

---

## 问题 4：Prompt 测试怎么写？

验证系统提示的组成部分是否正确：

```python
def test_self_update_section_included_for_custom_agents():
    """自定义 Agent 包含自更新指令"""
    config = AppConfig(custom_agent=True)
    prompt = build_system_prompt(config)
    assert "self_update" in prompt

def test_mounts_section_shows_correct_paths():
    """挂载点信息正确显示"""
    config = AppConfig(mounts=[
        PathMapping(container_path="/mnt/user-data", read_only=False)
    ])
    prompt = build_system_prompt(config)
    assert "/mnt/user-data" in prompt
    assert "read-write" in prompt

def test_no_skills_section_when_disabled():
    """禁用技能时不包含技能段落"""
    config = AppConfig(skills_enabled=False)
    prompt = build_system_prompt(config)
    assert "skills" not in prompt.lower()
```

---

## 问题 5：集成测试怎么评估质量？

集成测试需要调用真实 LLM，用**评估指标**代替固定断言：

| 指标 | 怎么测 | 合格线 |
|------|--------|-------|
| 任务完成率 | 预设任务，检查是否完成 | ≥ 80% |
| 工具正确率 | 检查工具选择是否合理 | ≥ 90% |
| 幻觉率 | 抽样检查事实准确性 | ≤ 10% |
| 引用准确率 | 检查引用是否指向真实来源 | ≥ 95% |
| 循环触发率 | 统计循环检测触发次数 | ≤ 5% |

```python
# 评估示例
def test_agent_handles_file_not_found():
    """Agent 遇到文件不存在时应先探索再操作"""
    result = run_agent("帮我编辑 config.yaml")

    # 评估: Agent 是否先 ls 再操作?
    tool_calls = extract_tool_calls(result)
    assert any(tc.name == "bash" and "ls" in tc.args.get("command", "")
               for tc in tool_calls)
    # 评估: 最终是否成功?
    assert result.final_message is not None
```

---

## 问题 6：非确定性测试怎么避免误报？

| 策略 | 做法 | 效果 |
|------|------|------|
| 温度设为 0 | `temperature: 0` | 最大化确定性 |
| 检查关键行为 | 不检查具体文本，检查是否调用了正确工具 | 容忍措辞变化 |
| 多次运行取平均 | 同一用例跑 3-5 次 | 过滤偶然失败 |
| A/B 对比 | 改动前后对比，不只看绝对值 | 检测回归 |

```python
# 稳定性测试: 同一任务跑多次
@pytest.mark.parametrize("run", range(3))
def test_stability(run):
    result = run_agent("列出当前目录文件")
    tool_calls = extract_tool_calls(result)
    # 不检查具体输出，检查行为模式
    assert any(tc.name == "bash" for tc in tool_calls)
```

---

## 问题 7：生产环境怎么持续评估？

三层监控：

```
自动指标（实时）
    ├── 工具错误率 > 20% → 告警
    ├── 循环检测触发 → 告警
    ├── 平均 token/Run 异常 → 告警
    └── LLM 调用失败率 > 5% → 告警

用户反馈（异步）
    ├── 点赞/点踩 → 质量信号
    ├── 重新提问 → 完成度信号
    └── 人工修改 Agent 输出 → 质量基准

定期审计（周/月）
    ├── 抽样 50 条对话人工评估
    ├── 按任务类型分类统计
    └── 对比不同时期的趋势
```

---

## 问题 8：如何评估 Prompt 变更的效果？

A/B 测试流程：

```
1. 准备评估集
   ├── 50 个典型任务
   ├── 每个 task 有明确的完成标准
   └── 覆盖不同难度和类型

2. 基线测试
   └── 用当前 Prompt 跑评估集，记录指标

3. 变更测试
   └── 用新 Prompt 跑同一评估集

4. 对比
   ├── 任务完成率: 80% → 85%? (↑5%)
   ├── 平均轮数: 8.2 → 7.5? (↓0.7)
   ├── 幻觉率: 12% → 8%? (↓4%)
   └── Token 消耗: 5.2k → 4.8k? (↓8%)
```

---

## 问题 9：评估指标体系怎么设计？

按维度分层：

| 维度 | 指标 | 采集方式 |
|------|------|---------|
| **效果** | 任务完成率 | 人工评估 / 自动检查 |
| **效率** | 平均轮数、Token/任务 | 自动统计 |
| **质量** | 幻觉率、引用准确率 | 抽样检查 |
| **安全** | 护栏触发率、循环检测率 | 自动统计 |
| **成本** | Token 单价、平均成本/任务 | 自动统计 |
| **体验** | 用户满意度、重试率 | 用户反馈 |

---

## 问题 10：测试和评估的完整清单？

| 阶段 | 测试类型 | 频率 | 工具 |
|------|---------|------|------|
| 开发 | 组件单测 | 每次提交 | pytest |
| 开发 | Prompt 测试 | 改 Prompt 时 | pytest + Mock |
| 预发布 | 集成评估 | 发版前 | 评估集 + LLM |
| 预发布 | 性能测试 | 发版前 | 负载测试 |
| 生产 | 自动监控 | 实时 | 指标 + 告警 |
| 生产 | 用户反馈 | 持续 | 点赞/点踩 |
| 生产 | 定期审计 | 周/月 | 人工评估 |

---

## 数据流概览

```
代码变更
    │
    ▼ 组件测试（确定性）
    ├── 中间件行为验证
    ├── 工具输入/输出验证
    └── 格式化逻辑验证
    │
    ▼ Prompt 测试（半确定性）
    ├── 组成部分验证
    ├── 条件包含验证
    └── 记忆注入验证
    │
    ▼ 集成评估（非确定性）
    ├── 评估集跑分
    ├── A/B 对比
    └── 多次运行稳定性
    │
    ▼ 生产监控
    ├── 自动指标告警
    ├── 用户反馈收集
    └── 定期人工审计
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| Prompt 测试 | `backend/tests/test_lead_agent_prompt.py` |
| 记忆注入测试 | `backend/tests/test_memory_prompt_injection.py` |
| 测试目录 | `backend/tests/` |

## 深入阅读

- [Agent 设计决策](../core/agent/06-design-decisions.md) — 测试策略
- [扩展指南](../guides/02-extension-guide.md) — 测试扩展
- [可观测性](027-Agent可观测性与调试.md) — 生产监控
- [Agent Prompt 工程](028-Agent-Prompt工程.md) — Prompt 设计
