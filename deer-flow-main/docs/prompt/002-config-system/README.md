# 002-配置系统模块

> 已验证来源：deer-flow 项目 `config/app_config.py` + `config/model_config.py` + `config/agents_config.py` + `config/paths.py`
> 本提示词可在新项目中直接使用，通过适配层注入新项目的配置格式和路径布局差异，不需要修改本提示词本体。

---

## 一、设计意图

**为什么需要这个模块？**

AI Agent 项目有大量运行时可调参数：模型列表及能力标识、工具分组、沙箱配置、智能体人格、记忆策略、流式桥接等。散落在环境变量和代码里的硬编码会导致：改错键名运行时才爆、结构变更无感知、多环境部署时配置覆盖混乱。

本模块把"读文件 → 解析 → 校验 → 环境变量注入 → 缓存 → 按需重载"收拢为一个类型安全的入口，调用方只接触 Pydantic 对象，不碰原始 dict。

**解决的核心痛点：**
- YAML 值拼错不报错 → Pydantic 校验在启动时拦截
- 环境变量注入散落各处 → 集中 `resolve_env_variables`，先于 Pydantic 执行
- 子系统配置耦合在根对象 → 各子系统独立 `load_xxx_from_dict()`，可单独 reload
- 配置修改需重启 → mtime 检测自动重载
- 多环境路径不一致 → 四级路径解析 + DooD 宿主机路径覆盖

---

## 二、输入契约

| 输入项 | 来源 | 说明 |
|--------|------|------|
| `config.yaml` | 文件系统 | 根配置文件，YAML 格式 |
| `config.example.yaml` | 文件系统 | 示例配置，用于版本对比（可选） |
| `DEER_FLOW_CONFIG_PATH` | 环境变量 | 指定配置路径（可选） |
| `DEER_FLOW_HOME` | 环境变量 | 数据根目录（可选） |
| `$VAR_NAME` | YAML 内占位 | 运行时解析为环境变量值 |

### 配置路径四级解析

```
构造参数 config_path                        ← 最高（测试注入）
    ↓ (None 时降级)
环境变量 DEER_FLOW_CONFIG_PATH              ← 容器部署
    ↓ (None 时降级)
当前目录 config.yaml                         ← 本地开发
    ↓ (不存在时降级)
父目录 config.yaml                           ← monorepo 子目录
```

---

## 三、输出契约

### 对外暴露的接口

```python
def get_app_config() -> AppConfig:           # 单例，mtime 自动重载
def reload_app_config(path=None) -> AppConfig  # 强制重载
def reset_app_config() -> None:               # 清缓存（测试用）
def set_app_config(config) -> None:           # 注入 mock（测试用）
def get_paths() -> Paths:                     # 数据目录布局单例
```

### AppConfig 提供的查询方法

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_model_config(name)` | `ModelConfig \| None` | 按名称查找模型配置 |
| `get_tool_config(name)` | `ToolConfig \| None` | 按名称查找工具配置 |
| `get_tool_group_config(name)` | `ToolGroupConfig \| None` | 按名称查找工具组配置 |

### 保证

| 保证项 | 说明 |
|--------|------|
| 返回值已通过 Pydantic 校验 | 字段类型和必填项在启动时即验证 |
| 环境变量已解析 | `$VAR` 已替换为实际值，不会泄漏到业务代码 |
| 文件变更自动生效 | mtime 变化后下次调用静默重载 |
| 路径穿越被阻止 | thread_id 正则校验 + `resolve_virtual_path` 双重验证 |

---

## 四、行为约束

### 约束 1：环境变量必须在 Pydantic 之前解析

```
正确：resolve_env_variables(config_data) → model_validate(processed_data)
错误：model_validate(config_data) → 事后替换 $VAR
```
`$OPENAI_API_KEY` 不是合法 URL，Pydantic 校验会失败。

### 约束 2：单例 + mtime，不用 watchdog

请求驱动型应用每次请求调一次 `get_app_config()` 即可。文件监听线程增加复杂度且在容器环境中不可靠。

### 约束 3：thread_id 必须正则校验

```python
_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
```
阻止 `/../` 穿越和特殊字符。`resolve_virtual_path` 还要用 `relative_to` 二次验证。

### 约束 4：子配置延迟加载，不耦合在根类

```python
# 正确：独立函数按需调用
if "memory" in config_data:
    load_memory_config_from_dict(config_data["memory"])

# 错误：全部声明在 AppConfig 字段中（新增子系统就要改根类）
```

### 约束 5：`set_app_config` 后冻结文件重载

注入 mock 后 `get_app_config()` 直接返回 mock，不读文件，不检查 mtime。直到 `reset_app_config()` 才恢复文件加载。

### 约束 6：沙箱目录 chmod 0o777

容器内进程 UID 可能与宿主机不同，`mkdir(mode=0o777)` 受 umask 影响，需要显式 `chmod(0o777)` 确保容器可写。

---

## 五、验证场景

| # | Given | When | Then |
|---|-------|------|------|
| 1 | 配置文件不存在 | `get_app_config()` | FileNotFoundError 含搜索路径 |
| 2 | YAML 中 `$MISSING_VAR` | 加载配置 | ValueError 提示变量名 |
| 3 | config_version < example | 启动 | warning 日志，不阻断 |
| 4 | 运行时修改配置文件 | 下次 `get_app_config()` | 自动重载 + info 日志 |
| 5 | `set_app_config(mock)` | `get_app_config()` | 返回 mock，不读文件 |
| 6 | `reset_app_config()` | `get_app_config()` | 重新从文件加载 |
| 7 | thread_id 含 `../` | `thread_dir()` | ValueError |
| 8 | 虚拟路径含穿越 | `resolve_virtual_path()` | ValueError |
| 9 | agent 目录无 config.yaml | `list_custom_agents()` | 跳过 + warning |

---

## 六、自由度与禁区

### 可以改的

- 配置格式（YAML / TOML / JSON）
- 子系统列表（按需增减 `load_xxx_from_dict`）
- 路径布局（`threads/` → `sessions/` 等）
- 环境变量前缀（`$` → `${}`）
- 单例策略（模块级变量 / 依赖注入容器）

### 不能改的

- **环境变量先于 Pydantic**：顺序反了会校验失败
- **路径安全双重验证**：正则 + `relative_to`，缺一个就有穿越风险
- **mtime 比较而非内容 hash**：mtime 够用且开销为零
- **子配置延迟加载**：耦合在根类则新增子系统必须改根类定义

---

## 七、依赖的上下游模块

```
[无上游] 配置系统是底层模块
    ↓
[下游] Agent 工厂 → get_app_config(), get_model_config()
[下游] 模型工厂 → ModelConfig 能力标识
[下游] 工具系统 → ToolConfig, ToolGroupConfig
[下游] 沙箱系统 → Paths, sandbox_work_dir()
[下游] 智能体系统 → load_agent_config(), load_agent_soul()
```
