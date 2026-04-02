# 13-工具函数系统模块文件清单

## 模块概述

**路径**：`backend/packages/harness/deerflow/utils/`

**核心作用**：提供通用工具函数

**设计理念**：纯函数设计 + 无副作用 + 可复用

## 文件清单

### 1. file_conversion.py
- **路径**：`utils/file_conversion.py`
- **核心函数**：
  - 文档转换（PDF, PPT, Excel, Word）
  - 图片 OCR
  - 格式转换
- **职责**：文件格式转换工具

### 2. network.py
- **路径**：`utils/network.py`
- **核心函数**：
  - HTTP GET/POST 封装
  - 重试机制
  - 超时处理
  - 多 URL 降级
- **职责**：网络请求工具

### 3. readability.py
- **路径**：`utils/readability.py`
- **核心函数**：
  - 可读性评分
  - 文本复杂度分析
  - 语言检测
- **职责**：文本可读性分析

## 核心设计模式

### 1. 纯函数设计
无副作用，易于测试和并发

### 2. 单一职责
每个文件只做一件事

### 3. 错误处理
统一的异常处理模式

## 关键依赖

- `httpx` - HTTP 客户端
- `markitdown` - 文档转换
- 文档处理库

## 相关模块

- **依赖**：无（基础模块）
- **被依赖**：所有模块
