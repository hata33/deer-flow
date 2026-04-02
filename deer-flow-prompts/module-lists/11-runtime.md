# 12-运行时系统模块文件清单

## 模块概述

**路径**：`backend/packages/harness/deerflow/runtime/`

**核心作用**：管理对话运行的完整生命周期

**设计理念**：Provider 模式 + 异步优先 + 流式桥接

## 文件清单

### 1. __init__.py
- **路径**：`runtime/__init__.py`
- **职责**：模块入口

### 2. serialization.py
- **路径**：`runtime/serialization.py`
- **核心函数**：
  - 消息序列化/反序列化
  - ThreadState 序列化
- **职责**：状态序列化工具

### 3. runs/ (4个文件)
- **路径**：`runtime/runs/`
- **文件**：
  - `__init__.py`
  - `manager.py` - RunManager 运行管理器
  - `schemas.py` - 运行数据模型
  - `worker.py` - Worker 执行器
- **职责**：运行生命周期管理

### 4. store/ (3个文件)
- **路径**：`runtime/store/`
- **文件**：
  - `__init__.py`
  - `provider.py` - Provider 接口
  - `async_provider.py` - 异步存储实现
  - `_sqlite_utils.py` - SQLite 工具
- **职责**：状态持久化

### 5. stream_bridge/ (4个文件)
- **路径**：`runtime/stream_bridge/`
- **文件**：
  - `__init__.py`
  - `base.py` - 流式桥接基类
  - `async_provider.py` - 异步提供者
  - `memory.py` - 内存实现
- **职责**：流式传输桥接

## 核心设计模式

### 1. Provider 模式
支持多种存储后端和流式协议

### 2. 异步优先
全异步设计，支持高并发

### 3. 流式桥接
解耦执行与传输

## 关键依赖

- `aiosqlite` - 异步 SQLite
- `langgraph` - 图执行引擎

## 相关模块

- **依赖**：01-配置系统, 12-工具函数
- **被依赖**：02-代理系统, 04-子代理系统
