# Agentic RAG 方案

**目标**: 将 DeerFlow Agent 改造为具备 Agentic RAG 能力的知识助手。不同于传统 RAG 的"一次性检索+生成"，Agentic RAG 让 Agent 自主决定何时检索、检索什么、检索几次，并结合推理和工具使用完成复杂知识任务。

---

## 1. 架构总览

```
用户: "我们公司的远程办公政策是什么？病假怎么算？"
    │
    ▼ 意图分析层（Agent 推理）
    ├── 是否需要检索？→ 是（涉及企业内部知识）
    ├── 检索哪些源？→ HR 政策文档 + 员工手册
    └── 检索策略？→ 两次检索：远程办公 + 病假
    │
    ▼ 检索层
    ├── 向量检索（语义相似度）
    ├── 关键词检索（精确匹配）
    └── 混合检索（两者融合 + 重排序）
    │
    ▼ 评估层
    ├── 相关性评分 → 过滤低质量结果
    ├── 信息充分性 → 是否需要追加检索
    └── 来源追溯 → 引用标注
    │
    ▼ 生成层
    ├── 基于检索结果生成回答
    ├── 不确定时标注 [不确定]
    └── 附加来源引用
```

---

## 2. 与传统 RAG 的区别

| 维度 | 传统 RAG | Agentic RAG（本方案） |
|------|---------|---------------------|
| 检索决策 | 固定 pipeline | Agent 自主判断 |
| 检索次数 | 1 次 | 按需多次 |
| 查询改写 | 无/简单 | Agent 推理后改写 |
| 结果评估 | 无 | Agent 评估相关性 |
| 工具组合 | 仅检索 | 检索 + Web 搜索 + 文件读取 |
| 纠错 | 无 | 检索失败自动换策略 |
| 来源 | 单一向量库 | 多源（向量 + 关键词 + Web） |

---

## 3. 需要新建的文件

```
backend/packages/harness/deerflow/
    ├── community/agentic_rag/             # Agentic RAG 工具集
    │   ├── __init__.py
    │   ├── tools.py                       # 核心工具定义
    │   ├── vector_store.py                # 向量存储封装
    │   ├── document_processor.py          # 文档处理（切片/嵌入）
    │   ├── retriever.py                   # 混合检索器
    │   └── reranker.py                    # 重排序器
    │
    ├── agents/middlewares/
    │   └── rag_context_middleware.py      # RAG 上下文管理
    │
    └── config/
        └── rag_config.py                 # 配置模型

skills/
    └── custom/agentic-rag/               # RAG 技能
        └── SKILL.md

docs/core/场景/Agentic-RAG/               # 文档（本目录）
```

---

## 4. 核心工具设计

### 4.1 工具清单

| 工具 | 功能 | 输入 | 输出 |
|------|------|------|------|
| `index_documents` | 索引文档到向量库 | 文件路径/目录 | 索引统计 |
| `search_knowledge` | 语义检索知识库 | 查询文本、top_k | 相关文档片段 + 来源 |
| `keyword_search` | 关键词检索 | 关键词、top_k | 匹配文档片段 + 来源 |
| `hybrid_search` | 混合检索 | 查询文本、top_k | 融合排序后的结果 |
| `list_collections` | 列出知识库集合 | 无 | 集合名 + 文档数 |
| `get_document_info` | 查看文档元数据 | 文档 ID | 文档信息 + 切片统计 |

### 4.2 工具实现骨架

```python
# community/agentic_rag/tools.py

from langchain_core.tools import tool
from typing import Optional


@tool(parse_docstring=True)
def search_knowledge(
    query: str,
    collection: str = "default",
    top_k: int = 5,
    score_threshold: float = 0.7,
) -> str:
    """在知识库中进行语义检索，返回与查询最相关的文档片段。

    当你需要查找内部知识（公司政策、技术文档、产品信息等）时使用此工具。

    Args:
        query: 搜索查询文本
        collection: 知识库集合名称，默认 "default"
        top_k: 返回的最大结果数
        score_threshold: 最低相似度阈值（0-1），低于此值的结果被过滤

    Returns:
        相关文档片段列表，包含内容和来源信息
    """
    from .retriever import get_retriever

    retriever = get_retriever(collection)
    results = retriever.semantic_search(
        query=query,
        top_k=top_k,
        score_threshold=score_threshold,
    )

    if not results:
        return "未找到相关结果。建议：尝试不同的关键词，或使用 keyword_search 精确匹配。"

    output = []
    for i, doc in enumerate(results, 1):
        source = doc.metadata.get("source", "未知来源")
        score = doc.metadata.get("score", 0)
        output.append(
            f"[结果 {i}] (相似度: {score:.2f}) 来源: {source}\n"
            f"{doc.page_content}"
        )

    return "\n\n".join(output)


@tool(parse_docstring=True)
def keyword_search(
    keywords: str,
    collection: str = "default",
    top_k: int = 5,
) -> str:
    """在知识库中进行关键词检索，适合精确匹配场景。

    当语义检索结果不理想时，可尝试用关键词检索获取更精确的匹配。

    Args:
        keywords: 搜索关键词
        collection: 知识库集合名称
        top_k: 返回的最大结果数

    Returns:
        匹配的文档片段列表
    """
    from .retriever import get_retriever

    retriever = get_retriever(collection)
    results = retriever.keyword_search(keywords=keywords, top_k=top_k)

    if not results:
        return "未找到匹配结果。建议：减少关键词数量，或使用 search_knowledge 语义搜索。"

    output = []
    for i, doc in enumerate(results, 1):
        source = doc.metadata.get("source", "未知来源")
        output.append(f"[结果 {i}] 来源: {source}\n{doc.page_content}")

    return "\n\n".join(output)


@tool(parse_docstring=True)
def hybrid_search(
    query: str,
    collection: str = "default",
    top_k: int = 5,
    semantic_weight: float = 0.7,
) -> str:
    """混合检索：同时使用语义检索和关键词检索，结果融合后重排序。

    综合了语义理解和精确匹配的优势，是推荐的默认检索方式。

    Args:
        query: 搜索查询文本
        collection: 知识库集合名称
        top_k: 返回的最大结果数
        semantic_weight: 语义检索权重（0-1），关键词检索权重为 1-此值

    Returns:
        融合排序后的文档片段列表
    """
    from .retriever import get_retriever

    retriever = get_retriever(collection)
    results = retriever.hybrid_search(
        query=query,
        top_k=top_k,
        semantic_weight=semantic_weight,
    )

    if not results:
        return "未找到相关结果。"

    output = []
    for i, doc in enumerate(results, 1):
        source = doc.metadata.get("source", "未知来源")
        score = doc.metadata.get("score", 0)
        method = doc.metadata.get("retrieval_method", "hybrid")
        output.append(
            f"[结果 {i}] (得分: {score:.2f}, 方式: {method}) 来源: {source}\n"
            f"{doc.page_content}"
        )

    return "\n\n".join(output)


@tool(parse_docstring=True)
def index_documents(
    path: str,
    collection: str = "default",
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> str:
    """将文档索引到知识库中。支持文件路径或目录。

    Args:
        path: 文件或目录路径
        collection: 目标集合名称
        chunk_size: 文档切片大小（字符数）
        chunk_overlap: 切片重叠大小

    Returns:
        索引统计信息
    """
    from .document_processor import DocumentProcessor

    processor = DocumentProcessor(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    stats = processor.index_path(path=path, collection=collection)

    return (
        f"索引完成: {stats['documents']} 个文档, "
        f"{stats['chunks']} 个切片, "
        f"集合: {collection}"
    )
```

---

## 5. 向量存储封装

```python
# community/agentic_rag/vector_store.py

from langchain_core.vectorstores import VectorStore
from langchain_core.documents import Document


class VectorStoreManager:
    """统一向量存储接口，支持多种后端。"""

    def __init__(self, backend: str = "chroma", **kwargs):
        self._backend = backend
        self._stores: dict[str, VectorStore] = {}

        if backend == "chroma":
            from langchain_chroma import Chroma
            self._store_class = Chroma
            self._persist_dir = kwargs.get("persist_directory", ".deer-flow/vector_store")
        elif backend == "faiss":
            from langchain_community.vectorstores import FAISS
            self._store_class = FAISS
            self._persist_dir = kwargs.get("persist_directory", ".deer-flow/vector_store")
        else:
            raise ValueError(f"Unsupported backend: {backend}")

    def get_store(self, collection: str, embeddings) -> VectorStore:
        if collection not in self._stores:
            self._stores[collection] = self._store_class(
                collection_name=collection,
                embedding_function=embeddings,
                persist_directory=self._persist_dir,
            )
        return self._stores[collection]

    def add_documents(self, collection: str, documents: list[Document], embeddings):
        store = self.get_store(collection, embeddings)
        store.add_documents(documents)

    def similarity_search(
        self, collection: str, query: str, embeddings, top_k: int = 5
    ) -> list[Document]:
        store = self.get_store(collection, embeddings)
        return store.similarity_search_with_relevance_scores(
            query, k=top_k
        )
```

---

## 6. 混合检索器

```python
# community/agentic_rag/retriever.py

from langchain_core.documents import Document
from .vector_store import VectorStoreManager


class HybridRetriever:
    """混合检索：语义 + 关键词 + 重排序。"""

    def __init__(self, vector_manager: VectorStoreManager, embeddings):
        self._vs = vector_manager
        self._embeddings = embeddings

    def semantic_search(
        self, query: str, collection: str = "default",
        top_k: int = 5, score_threshold: float = 0.7,
    ) -> list[Document]:
        results = self._vs.similarity_search(
            collection, query, self._embeddings, top_k=top_k * 2
        )
        # 过滤低分结果
        filtered = [
            doc for doc, score in results
            if score >= score_threshold
        ]
        for doc, score in results:
            doc.metadata["score"] = score
        return filtered[:top_k]

    def keyword_search(
        self, keywords: str, collection: str = "default", top_k: int = 5,
    ) -> list[Document]:
        store = self._vs.get_store(collection, self._embeddings)
        # 使用 Chroma/FAISS 的关键词搜索（where document filter）
        results = store.similarity_search(
            keywords, k=top_k,
        )
        return results

    def hybrid_search(
        self, query: str, collection: str = "default",
        top_k: int = 5, semantic_weight: float = 0.7,
    ) -> list[Document]:
        # 并行执行语义和关键词检索
        semantic_results = self.semantic_search(query, collection, top_k=top_k * 2)
        keyword_results = self.keyword_search(query, collection, top_k=top_k * 2)

        # 融合排序 (Reciprocal Rank Fusion)
        return self._rrf_merge(
            semantic_results, keyword_results,
            semantic_weight, top_k
        )

    def _rrf_merge(
        self, semantic: list, keyword: list,
        semantic_weight: float, top_k: int,
    ) -> list[Document]:
        """Reciprocal Rank Fusion 融合排序。"""
        scores = {}
        k = 60  # RRF 参数

        for rank, doc in enumerate(semantic):
            content_hash = hash(doc.page_content)
            scores[content_hash] = scores.get(content_hash, 0) + \
                semantic_weight / (k + rank + 1)

        keyword_weight = 1 - semantic_weight
        for rank, doc in enumerate(keyword):
            content_hash = hash(doc.page_content)
            scores[content_hash] = scores.get(content_hash, 0) + \
                keyword_weight / (k + rank + 1)

        # 按融合分数排序
        all_docs = {hash(d.page_content): d for d in semantic + keyword}
        sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        results = []
        for content_hash, score in sorted_items[:top_k]:
            doc = all_docs[content_hash]
            doc.metadata["score"] = score
            doc.metadata["retrieval_method"] = "hybrid"
            results.append(doc)

        return results
```

---

## 7. 文档处理器

```python
# community/agentic_rag/document_processor.py

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


class DocumentProcessor:
    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200):
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n## ", "\n### ", "\n\n", "\n", "。", ".", " "],
        )

    def load_file(self, path: str) -> list[Document]:
        """加载单个文件，返回 Document 列表。"""
        if path.endswith(".md"):
            from langchain_community.document_loaders import TextLoader
            loader = TextLoader(path)
        elif path.endswith(".pdf"):
            from langchain_community.document_loaders import PyPDFLoader
            loader = PyPDFLoader(path)
        elif path.endswith(".docx"):
            from langchain_community.document_loaders import Docx2txtLoader
            loader = Docx2txtLoader(path)
        else:
            from langchain_community.document_loaders import TextLoader
            loader = TextLoader(path)

        return loader.load()

    def split_documents(self, documents: list[Document]) -> list[Document]:
        """将文档切分为适当大小的片段。"""
        return self._splitter.split_documents(documents)

    def index_path(self, path: str, collection: str) -> dict:
        """索引文件或目录到向量库。"""
        import os

        # 收集文件
        if os.path.isfile(path):
            files = [path]
        elif os.path.isdir(path):
            files = []
            for root, _, filenames in os.walk(path):
                for f in filenames:
                    if f.endswith((".md", ".txt", ".pdf", ".docx")):
                        files.append(os.path.join(root, f))
        else:
            return {"documents": 0, "chunks": 0, "error": f"路径不存在: {path}"}

        # 加载 + 切片
        all_docs = []
        for f in files:
            docs = self.load_file(f)
            all_docs.extend(docs)

        chunks = self.split_documents(all_docs)

        # 写入向量库
        from .vector_store import VectorStoreManager
        from .tools import _get_embeddings

        vs = VectorStoreManager()
        embeddings = _get_embeddings()
        vs.add_documents(collection, chunks, embeddings)

        return {
            "documents": len(files),
            "chunks": len(chunks),
        }
```

---

## 8. Agentic RAG 技能定义

```markdown
---
name: agentic-rag
description: "Agentic RAG 知识检索助手。Agent 自主决定何时检索、检索什么、检索几次，结合推理完成复杂知识任务。"
allowed-tools:
  - search_knowledge
  - keyword_search
  - hybrid_search
  - index_documents
  - list_collections
  - get_document_info
  - web_search
  - web_fetch
  - read_file
  - ask_clarification
---

## Agentic RAG 技能指令

你是一个具备智能检索能力的知识助手。你可以自主决定何时、如何检索信息。

### 检索决策框架

在回答问题前，先评估：

1. **是否需要检索？**
   - 常识性问题 → 直接回答
   - 企业内部知识 → 必须检索
   - 最新信息 → Web 搜索
   - 不确定 → 先检索确认

2. **用什么策略检索？**
   - 概念性问题 → `search_knowledge`（语义检索）
   - 精确术语 → `keyword_search`（关键词检索）
   - 不确定 → `hybrid_search`（混合检索，推荐默认使用）

3. **结果够不够？**
   - 结果充分 → 生成回答
   - 结果不足 → 改写查询，追加检索
   - 完全无关 → 换检索策略（如从语义切到关键词）

### 多轮检索模式

复杂问题可能需要多轮检索：

```
问题: "公司远程办公政策对跨时区会议有什么规定？"

第 1 轮: hybrid_search("远程办公 跨时区 会议规定")
→ 找到远程办公政策总则，但缺少会议细节

第 2 轮: keyword_search("跨时区会议")
→ 找到会议管理细则中的相关条目

综合两轮结果 → 生成完整回答
```

### 引用规范

- 每个事实性陈述必须标注来源
- 格式: [来源: 文档名]
- 如果来自 Web 搜索: [citation: 标题](URL)
- 不确定的信息标注 [不确定]，并说明原因

### 检索失败处理

- 检索无结果 → 尝试不同的关键词或检索方式
- 结果不相关 → 分析原因，改写查询
- 知识库未覆盖 → 使用 web_search 补充
- 所有途径都失败 → 如实告知用户
```

---

## 9. 配置

```yaml
# config.yaml 新增
agentic_rag:
  enabled: true

  vector_store:
    backend: "chroma"                    # chroma / faiss / pinecone
    persist_directory: ".deer-flow/vector_store"
    embedding_model: "text-embedding-3-small"  # OpenAI
    embedding_api_key: "${OPENAI_API_KEY}"

  documents:
    chunk_size: 1000                     # 切片大小（字符）
    chunk_overlap: 200                   # 切片重叠
    max_file_size: 10485760              # 单文件最大 10MB
    supported_formats: [".md", ".txt", ".pdf", ".docx"]

  retrieval:
    default_method: "hybrid"             # semantic / keyword / hybrid
    top_k: 5                             # 默认返回结果数
    score_threshold: 0.7                 # 最低相似度
    semantic_weight: 0.7                 # 混合检索中语义权重
    max_retrieval_rounds: 3              # 单次回答最大检索轮数
```

---

## 10. Agent 推理流程

### 场景 1: 简单查询

```
用户: "公司的报销流程是什么？"
    │
    ▼ Thought: "这是企业内部知识，需要检索知识库"
    │
    ▼ Action: hybrid_search("公司报销流程")
    Observation: 3 个相关文档片段
    │
    ▼ Thought: "结果充分，可以回答"
    │
    ▼ 输出: "根据公司政策 [来源: 报销管理制度v2.pdf]，
            报销流程如下: 1. 填写报销单 2. 直属主管审批..."
```

### 场景 2: 多轮检索

```
用户: "我们产品 X 和竞品 Y 的技术参数对比"
    │
    ▼ Thought: "需要检索产品 X 的技术参数"
    │
    ▼ Action: hybrid_search("产品X 技术参数 规格")
    Observation: 找到产品 X 的参数表
    │
    ▼ Thought: "产品 X 的信息有了，但缺少竞品 Y 的信息。
               内部知识库可能没有竞品数据，尝试 Web 搜索"
    │
    ▼ Action: web_search("竞品Y 技术参数 规格 2026")
    Observation: 找到竞品 Y 的公开参数
    │
    ▼ Thought: "两方信息都有了，生成对比表"
    │
    ▼ 输出: 对比表格 + 分析 + 双重引用
```

### 场景 3: 检索失败恢复

```
用户: "量子计算在金融风控中的应用"
    │
    ▼ Action: hybrid_search("量子计算 金融风控")
    Observation: 未找到相关结果
    │
    ▼ Thought: "知识库没有覆盖这个主题。换关键词试试"
    │
    ▼ Action: keyword_search("量子 金融")
    Observation: 仍然无结果
    │
    ▼ Thought: "内部知识库确实没有。用 Web 搜索补充"
    │
    ▼ Action: web_search("quantum computing financial risk management 2026")
    Observation: 找到 5 篇相关文章
    │
    ▼ Thought: "信息足够了，但需要注意来源可靠性"
    │
    ▼ 输出: 综合回答 + 标注"此信息来自公开来源，非内部知识库"
```

---

## 11. 高级特性

### 11.1 查询改写

Agent 自然地改写查询，不需要额外模块：

```
用户: "怎么请假？"

Agent 推理: "用户问请假流程。内部文档可能用的是'休假'或'假期申请'等术语。
           先用语义搜索，它能理解同义词。"

Action: search_knowledge("请假 休假 假期申请 流程")
```

### 11.2 渐进式深入

```
第 1 轮: 检索概览 → 获得政策框架
第 2 轮: 检索具体条款 → 获得详细规则
第 3 轮: 检索例外情况 → 获得边界条件
```

每轮检索都基于上一轮的结果缩小范围。

### 11.3 知识库管理

```
# 索引新文档
Action: index_documents("/mnt/user-data/uploads/新员工手册.pdf")
→ "索引完成: 1 个文档, 45 个切片"

# 查看知识库状态
Action: list_collections()
→ "default: 120 个文档, 3500 个切片"

# 查看特定文档
Action: get_document_info("doc_abc123")
→ "新员工手册.pdf, 45 个切片, 索引时间: 2026-05-20"
```

---

## 12. 实施步骤

| 阶段 | 内容 | 预计工期 |
|------|------|---------|
| **Phase 1** | 向量存储封装（Chroma/FAISS） | 1-2 天 |
| **Phase 2** | 文档处理器（加载+切片+嵌入） | 1-2 天 |
| **Phase 3** | 检索工具（6 个工具） | 2-3 天 |
| **Phase 4** | 混合检索器 + RRF 融合 | 1-2 天 |
| **Phase 5** | 技能 SKILL.md + 配置 | 0.5 天 |
| **Phase 6** | 测试（检索质量、多轮、失败恢复） | 2-3 天 |
| **Phase 7** | 文档 + 示例 | 1 天 |

---

## 13. 依赖

```txt
# requirements-rag.txt
langchain-chroma>=0.2        # Chroma 向量库
langchain-community>=0.3     # 文档加载器
langchain-text-splitters>=0.3  # 文本切片
langchain-openai>=0.3        # OpenAI Embeddings
chromadb>=0.5                # ChromaDB
# 或 faiss-cpu>=1.8          # FAISS 替代
```

---

## 参考

- [Agentic RAG: Complete Guide 2026](https://workativ.com/ai-agent/blog/agentic-rag) — Agentic RAG 全景
- [Weaviate: What Is Agentic RAG](https://weaviate.io/blog/what-is-agentic-rag) — 架构设计
- [Complete Guide to RAG Architectures](https://atul4u.medium.com/the-complete-guide-to-rag-architectures-from-naive-to-agentic-c90c8a87cf56) — 从简单到 Agentic
- [DeerFlow 扩展指南](../../guides/02-extension-guide.md) — 工具/中间件扩展
- [DeerFlow 社区工具](../../core/community/00-overview.md) — 外部工具集成
