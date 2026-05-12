# DeerFlow - 统一开发环境

.PHONY: help config config-upgrade check install dev dev-daemon start stop up down clean docker-init docker-start docker-stop docker-logs docker-logs-frontend docker-logs-gateway

PYTHON ?= python
BASH ?= bash

# 检测操作系统以兼容 Windows
ifeq ($(OS),Windows_NT)
    SHELL := cmd.exe
endif

help:
	@echo "DeerFlow 开发命令："
	@echo "  make config          - 生成本地配置文件（如配置已存在则中止）"
	@echo "  make config-upgrade  - 将 config.example.yaml 的新字段合并到 config.yaml"
	@echo "  make check           - 检查所有必需工具是否已安装"
	@echo "  make install         - 安装所有依赖（前端 + 后端）"
	@echo "  make setup-sandbox   - 预拉取沙箱容器镜像（推荐）"
	@echo "  make dev             - 以开发模式启动所有服务（支持热重载）"
	@echo "  make dev-daemon      - 以守护进程模式启动所有服务（后台运行）"
	@echo "  make start           - 以生产模式启动所有服务（优化，无热重载）"
	@echo "  make stop            - 停止所有运行中的服务"
	@echo "  make clean           - 清理进程和临时文件"
	@echo ""
	@echo "Docker 生产环境命令："
	@echo "  make up              - 构建并启动生产 Docker 服务（localhost:2026）"
	@echo "  make down            - 停止并移除生产 Docker 容器"
	@echo ""
	@echo "Docker 开发环境命令："
	@echo "  make docker-init     - 拉取沙箱镜像"
	@echo "  make docker-start    - 启动 Docker 开发服务（根据 config.yaml 自动识别模式，localhost:2026）"
	@echo "  make docker-stop     - 停止 Docker 开发服务"
	@echo "  make docker-logs     - 查看 Docker 开发日志"
	@echo "  make docker-logs-frontend - 查看 Docker 前端日志"
	@echo "  make docker-logs-gateway - 查看 Docker 网关日志"

# 生成本地配置文件
config:
	@$(PYTHON) ./scripts/configure.py

# 升级配置文件（合并新字段）
config-upgrade:
	@./scripts/config-upgrade.sh

# 检查必需工具
check:
	@$(PYTHON) ./scripts/check.py

# 安装所有依赖
install:
	@echo "正在安装后端依赖..."
	@cd backend && uv sync
	@echo "正在安装前端依赖..."
	@cd frontend && pnpm install
	@echo "✓ 所有依赖安装完成"
	@echo ""
	@echo "=========================================="
	@echo "  可选：预拉取沙箱镜像"
	@echo "=========================================="
	@echo ""
	@echo "如果你计划使用基于 Docker/Container 的沙箱，可以预拉取镜像："
	@echo "  make setup-sandbox"
	@echo ""

# 预拉取沙箱 Docker 镜像（可选但推荐）
setup-sandbox:
	@echo "=========================================="
	@echo "  预拉取沙箱容器镜像"
	@echo "=========================================="
	@echo ""
	@IMAGE=$$(grep -A 20 "# sandbox:" config.yaml 2>/dev/null | grep "image:" | awk '{print $$2}' | head -1); \
	if [ -z "$$IMAGE" ]; then \
		IMAGE="enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"; \
		echo "使用默认镜像：$$IMAGE"; \
	else \
		echo "使用配置中的镜像：$$IMAGE"; \
	fi; \
	echo ""; \
	if command -v container >/dev/null 2>&1 && [ "$$(uname)" = "Darwin" ]; then \
		echo "检测到 macOS 上的 Apple Container，正在拉取镜像..."; \
		container pull "$$IMAGE" || echo "⚠ Apple Container 拉取失败，将尝试 Docker"; \
	fi; \
	if command -v docker >/dev/null 2>&1; then \
		echo "正在使用 Docker 拉取镜像..."; \
		if docker pull "$$IMAGE"; then \
			echo ""; \
			echo "✓ 沙箱镜像拉取成功"; \
		else \
			echo ""; \
			echo "⚠ 沙箱镜像拉取失败（本地沙箱模式下可忽略）"; \
		fi; \
	else \
		echo "✗ Docker 和 Apple Container 均不可用"; \
		echo "  请安装 Docker：https://docs.docker.com/get-docker/"; \
		exit 1; \
	fi

# 以开发模式启动所有服务（支持热重载）
dev:
ifeq ($(OS),Windows_NT)
	@call scripts\run-with-git-bash.cmd ./scripts/serve.sh --dev
else
	@./scripts/serve.sh --dev
endif

# 以生产模式启动所有服务（优化，无热重载）
start:
ifeq ($(OS),Windows_NT)
	@call scripts\run-with-git-bash.cmd ./scripts/serve.sh --prod
else
	@./scripts/serve.sh --prod
endif

# 以守护进程模式启动所有服务（后台运行）
dev-daemon:
	@./scripts/start-daemon.sh

# 停止所有服务
stop:
	@echo "正在停止所有服务..."
	@-pkill -f "langgraph dev" 2>/dev/null || true
	@-pkill -f "uvicorn app.gateway.app:app" 2>/dev/null || true
	@-pkill -f "next dev" 2>/dev/null || true
	@-pkill -f "next start" 2>/dev/null || true
	@-pkill -f "next-server" 2>/dev/null || true
	@-pkill -f "next-server" 2>/dev/null || true
	@nginx -c $(PWD)/docker/nginx/nginx.local.conf -p $(PWD) -s quit 2>/dev/null || true
	@sleep 1
	@-pkill -9 nginx 2>/dev/null || true
	@echo "正在清理沙箱容器..."
	@-./scripts/cleanup-containers.sh deer-flow-sandbox 2>/dev/null || true
	@echo "✓ 所有服务已停止"

# 清理
clean: stop
	@echo "正在清理..."
	@-rm -rf backend/.deer-flow 2>/dev/null || true
	@-rm -rf backend/.langgraph_api 2>/dev/null || true
	@-rm -rf logs/*.log 2>/dev/null || true
	@echo "✓ 清理完成"

# ==========================================
# Docker 开发环境命令
# ==========================================

# 初始化 Docker 容器并安装依赖
docker-init:
	@./scripts/docker.sh init

# 启动 Docker 开发环境
docker-start:
	@./scripts/docker.sh start

# 停止 Docker 开发环境
docker-stop:
	@./scripts/docker.sh stop

# 查看 Docker 开发日志
docker-logs:
	@./scripts/docker.sh logs

# 查看 Docker 前端日志
docker-logs-frontend:
	@./scripts/docker.sh logs --frontend

# 查看 Docker 网关日志
docker-logs-gateway:
	@./scripts/docker.sh logs --gateway

# ==========================================
# Docker 生产环境命令
# ==========================================

# 构建并启动生产服务
up:
	@./scripts/deploy.sh

# 停止并移除生产容器
down:
	@./scripts/deploy.sh down
