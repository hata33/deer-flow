# 技能系统启示

> 来源：`backend/packages/harness/deerflow/skills/`（types、parser、loader、validation、installer）

## 1. Markdown + YAML frontmatter 作为知识载体——零门槛、可读、可 Git

技能以 `SKILL.md` 文件为核心，YAML frontmatter 声明元数据（name、description、license），正文是 Markdown 格式的工作流指引。Agent 在 system prompt 中看到技能名称和路径列表，需要时通过 `read_file` 按需加载完整内容。技能可以是嵌套目录结构（`skills/public/area/sub-skill/SKILL.md`），`os.walk` 递归扫描自动发现。

不要发明自定义文件格式或二进制协议来承载知识。Markdown 是人类和 LLM 同时理解的最佳交集——开发者用任何编辑器就能编写和预览，Agent 直接读入 context 无需转换。frontmatter 提供结构化元数据，正文提供非结构化知识，两者共存于同一文件。技能的"渐进式加载"（先注入索引，按需读全文）和 [[002-tools]] 的延迟工具加载是同一思路：**索引常驻 context，内容按需拉取**。

## 2. 安装链的多层纵深防御——校验分散在每一步，每层只关心一件事

`.skill` 归档安装流程是：文件校验 → 安全解压 → frontmatter 校验 → 同名冲突检查 → 复制到 custom/。安全防护分散在各层：`is_unsafe_zip_member` 检查绝对路径和 `..` 遍历，`is_symlink_member` 跳过符号链接，`safe_extract_skill_archive` 在解压后二次校验路径不越界 + 逐块累计大小防 zip bomb，`_validate_skill_frontmatter` 校验名称格式（hyphen-case）、禁止 description 中的尖括号（防 prompt injection），安装前再检查 `../` 路径注入。每层只做自己的事，互不依赖。

不要把安全校验集中在一个"大检查"函数里。ZIP 归档是典型的攻击面，威胁模型多样（路径遍历、符号链接、zip bomb、内容注入）。分散校验让每一层可独立测试、独立演进。`safe_extract_skill_archive` 不依赖外部状态（纯函数），`_validate_skill_frontmatter` 也是纯函数，Gateway 和 Client 共用同一份逻辑。这是 [[003-prompt]] 中"三重纵深约束"在文件系统场景下的映射——**安全约束不要只靠一层，每层只做一件事**。

## 3. 纯业务逻辑模块剥离——Gateway 和 Client 共享，无 HTTP 依赖

`installer.py` 和 `validation.py` 都是纯 Python 模块，无 FastAPI 导入。Gateway 的路由和 `DeerFlowClient` 都调用同一个 `install_skill_from_archive` 和 `_validate_skill_frontmatter`。返回值是普通 `dict`，不依赖 Pydantic 模型。状态管理（extensions_config.json 的读写）也封装在独立函数中，调用方只需关心输入输出。

不要让业务逻辑依赖框架。如果安装逻辑写在 FastAPI 路由里，`DeerFlowClient` 就必须通过 HTTP 调用或复制代码。把核心逻辑抽取为纯函数模块，上层（HTTP 或进程内）只是薄薄一层适配。这和 [[004-client]] 的"双模式架构"是一致的——**纯逻辑下沉，框架适配上浮**，同一份代码服务于 HTTP 和进程内两种接入方式。

## 4. public / custom 双目录——框架内容和用户内容的生命周期隔离

技能目录分为 `skills/public/`（随仓库提交，Git 跟踪）和 `skills/custom/`（gitignored，用户自建或安装）。`load_skills` 统一扫描两个目录，对调用方透明。`install_skill_from_archive` 只写入 `custom/`，不触碰 `public/`，框架升级不会覆盖用户技能。

不要把用户生成内容和框架提供内容混在同一个目录里。混在一起后，`git pull` 升级可能覆盖用户修改，或者 `.gitignore` 整个目录导致框架技能丢失。双目录让框架和用户各自独立演进——框架升级只动 `public/`，用户安装只动 `custom/`。类似的模式在 `agents/` 目录也存在：默认 Agent 由代码定义，自定义 Agent 在独立目录中通过 `config.yaml` + `SOUL.md` 配置。

## 5. 跨进程配置一致性——每次从磁盘读取，不信任缓存

`load_skills` 中读取启用状态使用 `ExtensionsConfig.from_file()` 而非 `get_extensions_config()`。前者每次从磁盘读取最新文件，后者可能返回内存缓存。原因是 Gateway API 和 LangGraph Server 运行在不同进程中——用户通过 Gateway API 修改技能启用状态后写入 `extensions_config.json`，LangGraph Server 的缓存不会失效，只有从磁盘重新读取才能看到变更。

不要在跨进程场景中信任内存缓存。当多个进程共享同一个配置文件时，进程 A 的写入不会通知进程 B 的缓存失效。DeerFlow 的解法是"只读场景用缓存，写入后从磁盘重读"。同样的模式出现在 MCP 工具加载（`get_cached_mcp_tools` 用 mtime 检测变更）和 `DeerFlowClient`（`update_mcp_config` 后 `reload_extensions_config()`）。通用原则：**多进程共享配置时，读取端不信任缓存，写入端确保落盘**。

## 6. 宿主路径与容器路径的映射封装在数据类中

`Skill` 数据类不暴露宿主机文件路径给 Agent。`get_container_path()` 和 `get_container_file_path()` 方法将宿主机的 `skills/custom/my-skill/` 映射为 Agent 看到的 `/mnt/skills/custom/my-skill/`。prompt 注入时只使用容器路径，Agent 通过 `read_file("/mnt/skills/custom/my-skill/SKILL.md")` 读取，中间件和沙箱系统负责虚拟路径到物理路径的转换。

不要让 Agent 知道宿主机的真实文件路径。暴露真实路径会泄漏部署细节，且不同运行环境（本地开发 vs Docker）的路径结构不同。用数据类封装路径映射，Agent 只看到稳定的虚拟路径（`/mnt/skills/`），环境差异由沙箱中间件消化。这和 `thread_state.py` 中 `workspace_path` → `/mnt/user-data/workspace` 的映射是同一套虚拟路径体系。
