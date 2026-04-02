# 11-社区集成系统模块文件清单

## 模块概述

**路径**：`backend/packages/harness/deerflow/community/`

**核心作用**：集成第三方服务和工具，扩展 Agent 能力边界

**设计理念**：统一接口 + 独立包结构 + 可插拔架构

## 文件清单

### 1. aio_sandbox/ (6个文件)
- **路径**：`community/aio_sandbox/`
- **文件**：
  - `__init__.py` - 模块入口
  - `aio_sandbox_provider.py` - AIO 沙箱提供者
  - `aio_sandbox.py` - AIO 沙箱实现
  - `backend.py` - 后端接口
  - `local_backend.py` - 本地后端
  - `remote_backend.py` - 远程后端
  - `sandbox_info.py` - 沙箱信息
- **职责**：AIO 沙箱集成

### 2. ddg_search/ (2个文件)
- **路径**：`community/ddg_search/`
- **文件**：
  - `__init__.py`
  - `tools.py` - DuckDuckGo 搜索工具
- **职责**：DuckDuckGo 搜索集成

### 3. firecrawl/ (1个文件)
- **路径**：`community/firecrawl/`
- **文件**：
  - `tools.py` - Firecrawl 网页抓取工具
- **职责**：Firecrawl API 集成

### 4. image_search/ (2个文件)
- **路径**：`community/image_search/`
- **文件**：
  - `__init__.py`
  - `tools.py` - 图片搜索工具
- **职责**：图片搜索集成

### 5. infoquest/ (2个文件)
- **路径**：`community/infoquest/`
- **文件**：
  - `infoquest_client.py` - InfoQuest 客户端
  - `tools.py` - InfoQuest 工具
- **职责**：InfoQuest 服务集成

### 6. jina_ai/ (2个文件)
- **路径**：`community/jina_ai/`
- **文件**：
  - `jina_client.py` - Jina AI 客户端
  - `tools.py` - Jina AI 工具
- **职责**：Jina AI 集成（Reader API, Embedding）

### 7. tavily/ (1个文件)
- **路径**：`community/tavily/`
- **文件**：
  - `tools.py` - Tavily 搜索工具
- **职责**：Tavily 搜索集成

## 核心设计模式

### 1. 统一工具接口
所有集成都通过 `Tool` 接口暴露给 Agent

### 2. 客户端封装
每个服务都有独立的客户端类处理 API 调用

### 3. 独立包结构
每个服务独立目录，可选依赖管理

## 关键依赖

- `httpx` - HTTP 客户端
- 第三方服务 SDK

## 相关模块

- **依赖**：08-工具系统, 12-工具函数
- **被依赖**：02-代理系统（通过工具注册）
