# 配置系统完整生命周期

## 一、应用启动阶段

```
Gateway 启动（app/gateway/app.py）
    │
    ▼
get_app_config()                         ← 首次调用，触发加载
    │
    ├── _app_config is None → 需要加载
    │
    ├── resolve_config_path()
    │   ├── DEER_FLOW_CONFIG_PATH？
    │   ├── 项目根目录 config.yaml？
    │   └── 传统位置回退？
    │
    └── _load_and_cache_app_config()
        │
        ├── AppConfig.from_file()
        │   ├── yaml.safe_load() → 原始字典
        │   ├── _check_config_version() → 版本比对
        │   ├── resolve_env_variables() → $VAR 解析
        │   ├── _apply_database_defaults() → 填充默认
        │   ├── ExtensionsConfig.from_file() → 加载 MCP/技能
        │   ├── model_validate() → Pydantic 校验
        │   ├── _validate_acp_agents() → ACP 校验
        │   └── _apply_singleton_configs() → 分发到子系统
        │       ├── title 配置 → _title_config
        │       ├── summarization → _summarization_config
        │       ├── memory → _memory_config
        │       ├── subagents → _subagents_config
        │       ├── guardrails → _guardrails_config
        │       ├── checkpointer → _checkpointer_config
        │       ├── stream_bridge → _stream_bridge_config
        │       ├── acp → _acp_agents
        │       └── checkpointer 变更？
        │           └── reset_checkpointer() + reset_store()
        │
        ├── _app_config = 配置实例
        ├── _app_config_path = 文件路径
        ├── _app_config_mtime = 文件 mtime
        └── return _app_config
```

## 二、配置热更新生命周期

```
管理员编辑 config.yaml
    │
    ▼
下次 get_app_config() 调用
    │
    ├── resolve_config_path() → 获取当前路径
    ├── _get_config_mtime() → 获取当前 mtime
    │
    ├── _app_config_mtime != current_mtime → 文件已变更
    │
    ├── 记录日志: "Config file has been modified (mtime: old → new)"
    │
    └── _load_and_cache_app_config() → 重新加载
        └── 全流程重新执行（YAML → 校验 → 分发）
```

### Extensions 配置热更新

```
Gateway API: PUT /api/mcp/config → 更新 extensions_config.json
    │
    ▼
MCP 模块下次加载工具时
    │
    ├── get_cached_mcp_tools()（mcp/cache.py）
    ├── _is_cache_stale() → mtime 变更 → 过期
    ├── reset_mcp_tools_cache() → 清空
    └── 懒加载触发 → ExtensionsConfig.from_file() → 新配置
```

## 三、ContextVar 覆盖生命周期

```
测试开始
    │
    ├── push_current_app_config(test_config)
    │   ├── 保存当前配置到栈
    │   └── 设置 ContextVar = test_config
    │
    ├── 测试运行
    │   └── get_app_config() → 返回 test_config
    │       （不检查 mtime，不读取文件）
    │
    └── pop_current_app_config()
        ├── 从栈中恢复原配置
        └── 回到文件缓存模式
```

## 四、环境变量解析生命周期

```
config.yaml 原始值
    │
    ▼
AppConfig.resolve_env_variables(data)
    │
    ├── 遇到 str 值
    │   ├── 以 $ 开头？
    │   │   ├── os.getenv(key) 有值 → 替换为环境变量值
    │   │   └── os.getenv(key) 无值 → ValueError
    │   └── 不以 $ 开头 → 原样保留
    │
    ├── 遇到 dict → 递归处理所有 value
    ├── 遇到 list → 递归处理所有 item
    └── 遇到其他类型 → 原样保留

extensions_config.json 原始值
    │
    ▼
ExtensionsConfig.resolve_env_variables(data)
    │
    ├── 与 AppConfig 版本相同逻辑
    └── 唯一区别：未找到的环境变量 → 空字符串（而非 ValueError）
        （扩展配置中的 $VAR 可能是可选的）
```

## 五、子系统配置分发生命周期

```
AppConfig 加载完成
    │
    ▼
_apply_singleton_configs(config, acp_agents)
    │
    ├── 保存旧 checkpointer 配置
    │
    ├── 分发所有子配置到全局单例
    │   ├── title → load_title_config_from_dict()
    │   ├── summarization → load_summarization_config_from_dict()
    │   ├── memory → load_memory_config_from_dict()
    │   ├── agents_api → load_agents_api_config_from_dict()
    │   ├── subagents → load_subagents_config_from_dict()
    │   │   └── 记录覆盖和自定义代理摘要到日志
    │   ├── tool_search → load_tool_search_config_from_dict()
    │   ├── guardrails → load_guardrails_config_from_dict()
    │   ├── checkpointer → load_checkpointer_config_from_dict()
    │   ├── stream_bridge → load_stream_bridge_config_from_dict()
    │   └── acp_agents → load_acp_config_from_dict()
    │       └── 记录 ACP 代理数量和名称到日志
    │
    └── checkpointer 配置变更？
        ├── 是 → reset_checkpointer() + reset_store()
        │   └── 下次使用时会用新配置重新创建
        └── 否 → 不需要重置
```

## 六、运行时配置访问生命周期

```
Agent 运行时需要配置
    │
    ▼
get_app_config()
    │
    ├── ContextVar 有覆盖？
    │   └── 是 → 返回覆盖配置（测试/运行时注入）
    │
    ├── 自定义配置？
    │   └── 是 → 返回自定义配置（不自动刷新）
    │
    └── 文件缓存模式
        ├── 解析当前路径和 mtime
        ├── 路径或 mtime 变更？
        │   └── 是 → 重新加载
        └── 返回缓存或新加载的配置
```

## 七、路径解析生命周期

```
Agent 创建线程
    │
    ▼
Paths.ensure_thread_dirs(thread_id, user_id=user_id)
    │
    ├── sandbox_work_dir → {user_dir}/threads/{id}/user-data/workspace/
    ├── sandbox_uploads_dir → {user_dir}/threads/{id}/user-data/uploads/
    ├── sandbox_outputs_dir → {user_dir}/threads/{id}/user-data/outputs/
    └── acp_workspace_dir → {user_dir}/threads/{id}/acp-workspace/
    │
    ├── mkdir(parents=True, exist_ok=True)
    └── chmod(0o777) → 确保沙箱容器可写入


Agent 工具使用虚拟路径
    │
    ▼
Paths.resolve_virtual_path(thread_id, "/mnt/user-data/outputs/report.pdf")
    │
    ├── 去除前导斜杠: "mnt/user-data/outputs/report.pdf"
    ├── 检查前缀匹配: "mnt/user-data"
    ├── 提取相对路径: "outputs/report.pdf"
    ├── 拼接: {thread_dir}/user-data/outputs/report.pdf
    ├── resolve() → 绝对路径
    └── 路径遍历检测: actual.relative_to(base)
```

## 八、错误处理生命周期

```
配置加载错误
    │
    ├── config.yaml 不存在 → FileNotFoundError
    ├── YAML 语法错误 → yaml.YAMLError（pydantic 校验前）
    ├── 环境变量未找到 → ValueError（config.yaml）
    │   └── $VAR 引用的环境变量不存在
    ├── Pydantic 校验失败 → ValidationError
    │   └── 必需字段缺失、类型不匹配等
    ├── extensions_config.json 不存在 → 空配置（不报错）
    ├── extensions_config.json JSON 语法错误 → ValueError
    │
    └── 所有错误都阻止应用启动（fail-fast 策略）

运行时配置访问错误
    │
    ├── mtime 检测到变更 → 自动重新加载
    │   └── 加载失败 → 异常传播（调用方处理）
    └── ContextVar 覆盖 → 不检查文件，不触发加载
```

## 九、配置版本管理生命周期

```
开发者修改 config schema
    │
    ├── 更新 config.example.yaml
    │   └── 递增 config_version: N+1
    │
    ├── 添加新字段的默认值
    │   └── 确保旧配置仍能加载（向后兼容）
    │
    └── 用户启动应用
        │
        ├── _check_config_version()
        │   ├── 读取用户的 config_version
        │   ├── 读取 config.example.yaml 的 config_version
        │   └── 用户 < 示例 → 警告日志
        │
        └── 用户运行 make config-upgrade
            └── 自动合并 config.example.yaml 的新字段
```
