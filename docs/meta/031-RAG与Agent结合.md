# RAG 与 Agent 结合

**问题**: Agent 只知道训练数据中的知识，对于最新信息、企业私有数据、实时动态一无所知。需要检索增强生成（RAG）来扩展 Agent 的知识边界。

---

## 问题 1：Agent 中的 RAG 和传统 RAG 有什么区别？

| 维度 | 传统 RAG | Agent RAG |
|------|---------|-----------|
| 检索时机 | 固定（查询前一次性检索） | 动态（Agent 自主决定何时检索） |
| 检索策略 | 预设 pipeline | Agent 选择搜索工具和策略 |
| 结果处理 | 直接拼入上下文 | Agent 分析后决定是否需要更多信息 |
| 迭代 | 通常单次检索 | 可多轮检索，逐步深入 |

Agent RAG 的核心优势：**Agent 自主决定搜什么、搜几次、怎么用结果**。

---

## 问题 2：DeerFlow 的搜索工具有哪些？

多 Provider 支持，可按成本和质量选择：

| Provider | 特点 | 成本 |
|----------|------|------|
| Tavily | 高质量结构化结果 | 付费（有免费额度） |
| DuckDuckGo | 免费，无需 API Key | 免费 |
| Serper | Google SERP 结果 | 付费 |
| Exa | AI 优化语义搜索 | 付费 |

```yaml
community:
  search:
    provider: "tavily"  # 或 duckduckgo / serper / exa
    api_key: "${TAVILY_API_KEY}"
```

所有 Provider 返回统一的 JSON 格式：

```json
{
  "title": "页面标题",
  "url": "https://example.com",
  "snippet": "内容摘要"
}
```

---

## 问题 3：网页内容提取工具有哪些？

搜索到 URL 后需要提取内容：

| 工具 | 特点 | 适用场景 |
|------|------|---------|
| Jina AI | 异步 + Readability | 快速提取正文 |
| Firecrawl | 专业抓取 + Markdown | 复杂网页 |
| InfoQuest | 搜索+抓取+图片搜索 | 一站式研究 |

```yaml
community:
  web_fetch:
    provider: "jina"  # 或 firecrawl / infoquest
```

Agent 的工作流：`搜索 → 拿到 URL → 提取内容 → 分析`。

---

## 问题 4：引用系统怎么实现？

系统提示强制引用：

```
引用规范:
- 使用外部信息必须标注来源
- 格式: [citation:标题](URL)
- 回复末尾附完整来源列表
- 禁止编造不存在的来源
```

搜索工具返回结构化数据，Agent 据此生成引用：

```
Agent 输出:
"根据最新资料，React 19 引入了 Server Components [citation:React 19 Blog](https://react.dev/blog/...)。

主要新特性包括:
1. Server Components — [citation:React Docs](https://react.dev/reference/...)
2. Actions — [citation:React Blog](https://react.dev/blog/...)

来源:
- [React 19 Blog](https://react.dev/blog/...)
- [React Docs](https://react.dev/reference/...)"
```

---

## 问题 5：Agent 如何决定何时检索？

没有硬编码的触发条件——Agent 自主判断：

```
用户: "React 19 有什么新特性？"

Agent 的推理:
Thought: "React 19 是近期发布的，我的训练数据可能不包含最新信息。
         需要搜索获取准确内容。"
Action: web_search("React 19 new features 2026")
Observation: 搜索结果...

vs.

用户: "帮我写一个快速排序"

Agent 的推理:
Thought: "快速排序是经典算法，我直接就能写。不需要搜索。"
Action: write_file("sort.py", content="def quicksort(arr): ...")
```

关键：Agent 不是每次都搜索，而是**在知识不足时才搜索**。

---

## 问题 6：多轮检索怎么做？

Agent 可以逐步深入：

```
第 1 轮: 搜索概览
web_search("React 19 新特性") → 获得 5 个结果的摘要

第 2 轮: 深入阅读
web_fetch("https://react.dev/blog/react-19") → 获得详细内容

第 3 轮: 补充细节
web_search("React 19 Server Components 教程") → 获得实践指南

第 4 轮: 综合分析
"基于以上信息，给用户一个完整的总结"
```

每一步都是 Agent 自主决策——搜索什么、搜几次、何时停止。

---

## 问题 7：本地知识库怎么做 RAG？

DeerFlow 的文件上传 + 技能系统可以充当简单的本地 RAG：

```
方案 1: 文件上传
用户上传 docs/ → 转为 Markdown → 注入 Agent 上下文
Agent 可以 read_file("/mnt/user-data/uploads/doc.md") 按需读取

方案 2: 技能目录
企业知识放在 skills/knowledge-base/SKILL.md
Agent 加载技能后获得领域知识
```

不是向量数据库级别的 RAG，但对于中小规模文档足够用。

---

## 问题 8：搜索结果的质量怎么保证？

多层过滤：

| 层 | 做什么 | 怎么做 |
|----|--------|-------|
| Provider 层 | 高质量结果 | 选 Tavily/Exa 等优质 Provider |
| Agent 层 | 筛选相关结果 | Agent 判断哪些结果与任务相关 |
| 引用层 | 来源可追溯 | 强制引用格式 |
| 时效层 | 结果时效性 | 搜索参数中指定时间范围 |

---

## 问题 9：RAG 的成本怎么控制？

| 优化 | 效果 |
|------|------|
| 按需搜索（不是每次都搜） | 避免不必要的 API 调用 |
| 先搜索摘要，再深入阅读 | 先用 snippet 判断相关性 |
| 结果缓存 | 相同查询不重复搜索 |
| 限制搜索次数 | 循环检测防止无限搜索 |

---

## 问题 10：RAG + Agent 的完整工作流？

```
用户: "对比 React 和 Vue 的最新版本"
    │
    ▼ Agent 判断需要外部信息
    │
    ▼ 搜索 React 最新信息
web_search("React 19 features") → 结果列表
    │
    ▼ 深入阅读
web_fetch("https://react.dev/blog/...") → React 详情
    │
    ▼ 搜索 Vue 最新信息
web_search("Vue 3.5 features") → 结果列表
    │
    ▼ 深入阅读
web_fetch("https://vuejs.org/...") → Vue 详情
    │
    ▼ 综合分析 + 生成引用
"React 19 和 Vue 3.5 对比:
  ...
  来源: [React Blog](...), [Vue Docs](...)"
```

---

## 数据流概览

```
用户提问
    │
    ▼ Agent 判断知识是否充分
    │
    ├── 充分 → 直接回答
    │
    └── 不充分 → 搜索
        │
        ▼ 选择搜索工具
        ├── web_search → 获得结果列表
        └── web_fetch  → 提取页面内容
        │
        ▼ Agent 分析结果
        │
        ├── 信息不足 → 继续搜索
        │
        └── 信息充足 → 生成回答 + 引用
```

---

## 源码位置

| 内容 | 文件 |
|------|------|
| 搜索工具 | `backend/packages/harness/deerflow/community/` |
| Web 抓取 | `backend/packages/harness/deerflow/community/` |
| 搜索 Provider 配置 | `backend/packages/harness/deerflow/config/` |
| 系统提示（引用规范） | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` |

## 深入阅读

- [搜索工具](../core/community/01-search-tools.md) — 搜索 Provider 详解
- [Web 工具](../core/community/02-web-tools.md) — 内容提取工具
- [文件上传流程](014-文件上传流程.md) — 本地文档处理
- [技能加载](006-技能加载链路.md) — 领域知识注入
