# 002-配置系统

## 解决什么问题

项目需要从 YAML 文件加载配置，但直接用 `yaml.safe_load` 拿到的是无类型 dict。
运行时访问 `config["models"][0]["use"]` 拼错键名不报错、结构变更时无感知、环境变量注入散落在各处。
本模块把"读文件 → 解析 → 校验 → 缓存 → 按需重载"收拢为一个类型安全的入口。

## 本模块的职责边界

**只负责加载、解析、缓存、查询**：读 YAML → Pydantic 校验 → 全局单例缓存。
不负责：用配置做什么（工厂、中间件等模块的事）、文件内容生成（部署工具的事）、热重载推送（只检测 mtime 变化，不做 watch）。

## 不可变的设计决策

**YAML + Pydantic，而非 JSON Schema 或 dataclass**：YAML 可读性高、支持注释；Pydantic 提供运行时校验 + IDE 补全 + `extra="allow"` 前向兼容。两者组合让用户改配置不需要查文档。

**四级配置路径解析**：构造参数 → `DEER_FLOW_CONFIG_PATH` 环境变量 → 当前目录 `config.yaml` → 父目录 `config.yaml`。
四级对应四个场景：测试注入、容器部署、本地开发、monorepo 子目录运行。去掉任何一级都会在某条路径上报 FileNotFoundError。

**环境变量先于 Pydantic 解析**：`$OPENAI_API_KEY` 在 YAML 层被 `resolve_env_variables` 递归替换为实际值，然后才传给 `model_validate`。
顺序反过来会导致 Pydantic 校验失败（`$VAR` 不是合法的 URL/路径）。

**子配置延迟加载（lazy load）**：`title`、`summarization`、`memory`、`subagents` 等各有独立 `load_xxx_from_dict()` 函数，
在 `from_file` 中按需调用。好处：子系统可以独立 `reload_xxx()` 而不重载整个配置；新增子系统不改 AppConfig 类本身。

**单例 + mtime 自动重载**：`get_app_config()` 缓存实例，每次调用比较文件 mtime。变了就静默重载并打印 info 日志。
为什么不用 watchdog：项目是请求驱动型，每次请求入口调一次 `get_app_config()` 即可，不需要文件监听线程。

**版本过期警告**：`_check_config_version` 对比 `config.yaml` 和 `config.example.yaml` 的 `config_version` 字段。用户配置落后时 warning，不阻断启动。

**Agent 配置与 SOUL 分离**：`config.yaml` 存结构化参数（model、tool_groups），`SOUL.md` 存非结构化人格描述。
原因：人格文本需要 Markdown 格式和频繁编辑，不适合嵌在 YAML 字符串里。

**Paths 类的路径安全**：`thread_id` 用正则 `^[A-Za-z0-9_-]+$` 校验，阻止路径穿越。`resolve_virtual_path` 在 resolve 后用 `relative_to` 二次验证。
沙箱容器内的虚拟路径 `/mnt/user-data/` 必须映射到宿主机，但映射过程不能暴露宿主机其他目录。

**Host 路径覆盖（DooD 模式）**：`host_base_dir` 属性读 `DEER_FLOW_HOST_BASE_DIR` 环境变量。Docker-in-Docker 场景下 Docker daemon 在宿主机运行，volume mount 路径需要宿主机视角。

## 适配层

```yaml
<ADAPT>
# === 配置格式 ===
config_format: "yaml"                    # yaml / toml / json
config_class: "AppConfig"                # 根配置 Pydantic 类名
config_file_name: "config.yaml"          # 配置文件名
example_file_name: "config.example.yaml" # 示例配置文件名（用于版本对比）

# === 环境变量 ===
env_config_path: "DEER_FLOW_CONFIG_PATH" # 指定配置路径的环境变量名
env_home_dir: "DEER_FLOW_HOME"           # 指定数据根目录的环境变量名
env_host_base_dir: "DEER_FLOW_HOST_BASE_DIR" # DooD 宿主机路径覆盖
env_var_prefix: "$"                      # YAML 中环境变量的前缀

# === 子系统（按需启用）===
sub_configs:
  - name: "title"           # 对话标题生成
    loader: "load_title_config_from_dict"
  - name: "summarization"   # 长对话摘要
    loader: "load_summarization_config_from_dict"
  - name: "memory"          # 持久化记忆
    loader: "load_memory_config_from_dict"
  - name: "subagents"       # 子智能体调度
    loader: "load_subagents_config_from_dict"
  - name: "guardrails"      # 输入/输出护栏
    loader: "load_guardrails_config_from_dict"
  - name: "checkpointer"    # 状态持久化
    loader: "load_checkpointer_config_from_dict"
  - name: "acp_agents"      # 外部智能体协议
    loader: "load_acp_config_from_dict"
  - name: "tool_search"     # 工具搜索/延迟加载
    loader: "load_tool_search_config_from_dict"
  - name: "stream_bridge"   # 流式桥接
    loader: "load_stream_bridge_config_from_dict"

# === 路径布局 ===
virtual_path_prefix: "/mnt/user-data"    # 沙箱内虚拟路径前缀
thread_id_pattern: "^[A-Za-z0-9_-]+$"    # 线程ID合法字符
thread_dirs:                              # 每个线程自动创建的子目录
  - "workspace"
  - "uploads"
  - "outputs"
</ADAPT>
```

## 自检清单

| # | 验证 | 期望 |
|---|------|------|
| 1 | 配置文件不存在 | FileNotFoundError，含明确搜索路径 |
| 2 | YAML 中 `$MISSING_VAR` | ValueError，提示变量名 |
| 3 | config_version 低于 example | warning 日志，不阻断 |
| 4 | 运行时修改配置文件 | 下次 `get_app_config()` 自动重载 + info 日志 |
| 5 | `set_app_config(mock)` 后调用 `get_app_config()` | 返回 mock，不读文件 |
| 6 | `reset_app_config()` 后调用 `get_app_config()` | 重新从文件加载 |
| 7 | thread_id 含 `/` 或 `..` | ValueError 拒绝 |
| 8 | `resolve_virtual_path("/mnt/user-data/../../etc/passwd")` | ValueError 路径穿越 |
| 9 | agent 目录无 `config.yaml` | `list_custom_agents()` 跳过，warning |
| 10 | `ModelConfig` 含未知字段 | 不报错（`extra="allow"`） |

## 依赖模块

| 模块 | 本模块调用的接口 |
|------|----------------|
| **无外部依赖** | 配置系统是底层模块，被其他所有模块依赖 |

---

## 参考源码

以下是已验证的源码实现。阅读并理解其模式，然后根据 `<ADAPT>` 适配参数在新项目中重写。
不要复制粘贴——重点关注每个函数的**决策分支**和**边界条件处理**。

### 文件清单（按阅读顺序）

| 文件 | 职责 | 阅读重点 |
|------|------|---------|
| `model_config.py` | 单个模型的配置定义 | Pydantic `extra="allow"` 前向兼容；`supports_thinking` / `supports_vision` 能力标识如何影响下游工厂的降级逻辑 |
| `agents_config.py` | 自定义智能体配置加载 | `load_agent_config` 的两层校验（名称正则 + 文件存在）；`load_agent_soul` 的 None 回退；`list_custom_agents` 的容错遍历 |
| `paths.py` | 目录布局 + 路径安全 | `thread_id` 正则校验；`resolve_virtual_path` 的 resolve + `relative_to` 二次防穿越；`ensure_thread_dirs` 的 `chmod 0o777`（容器 UID 不匹配场景） |
| `app_config.py` | 根配置 + 单例缓存 | `from_file` 中子配置的延迟加载顺序；`resolve_env_variables` 的递归解析；`_check_config_version` 的向上搜索 example 文件；`get_app_config` 的 mtime 比较重载逻辑 |

源码文件见同目录下的 `src/` 子文件夹。
