# Fersk Codex

主项目通过飞书消息驱动 Codex，持久化配置与工作区从宿主机 `~/.fersk`、`~/.codex` 挂载读取。部署方式见[根目录说明](../README.md)。

主项目直接使用 `AsyncCodex()`，由你通过 Codex CLI 管理挂载的用户配置；代码不注册扩展或覆盖其连接设置。两个服务共用本项目 `configs/` 下的 `config_default.json`、`config_schema.json`、`validation.py` 和 `initialization.py`，启动时完整校验，主项目不消费 `mcp` 段。共享源仍归属于本项目，MCP 使用符号链接引用。

群聊机器人身份使用 `lark.credentials.robotUnionIdEnv` 指定环境变量名，默认读取 `LARK_ROBOT_UNION_ID`，并与飞书消息中的 `mention.id.union_id` 匹配；`robotNameEnv` 可作为名称匹配后备。

在已安装锁定依赖的环境中使用 `python -m fersk_codex.main` 启动。离线测试从本项目目录运行 `python -B tests/run_tests.py`。

工作区只在缺少 `.git`（目录或 worktree 文件）时初始化仓库。Git 初始化采用异步子进程，超时取 `codex.gitInitTimeoutSeconds`（默认 10 秒），取消或超时后回收子进程；初始化失败不提交模型任务。附件响应读取、校验和文件写入移至工作线程。用量仅写入 SQLite，并按任务回填耗时，不再生成 CSV。

Docker 从仓库根目录执行 `docker build -f fersk_codex/Dockerfile -t fersk-codex .`。容器首次启动原子初始化共享挂载配置，已有配置不覆盖。

Codex 执行实现位于 `codex/`，会话实现位于 `session/`：

- `codex_runtime.py`：SDK 生命周期、共享运行状态、停止、steer 和资源清理。
- `session/session_codex.py`：会话恢复、重置、名称初始化和时间同步。
- `codex_execution.py`：定义 `FerskCodex`，负责模型路由、任务执行、事件流、重试、错误转换及用量记录。

业务代码通过 `from fersk_codex.codex.codex_execution import FerskCodex, LiveTurn` 导入，
原有包级 `from fersk_codex import FerskCodex, LiveTurn` 仍采用惰性导出。
`FerskCodex` 继承会话管理和运行控制职责，所有操作共享运行状态；gateway 等调用方直接导入该类。
测试 mock 应指向依赖实际所在的职责模块；线程绑定统一通过 `thread_manager` 模块访问。
旧 `fersk_codex.core` 包路径已迁移至 `fersk_codex.codex`。

网关启动入口为根目录 `main.py`，支持 `python -m fersk_codex.main`、`python main.py`，
以及安装后的 `fersk-codex` 命令；Docker 使用同一模块入口。原 `gateway.py` 已移除。

- `main.py`：创建共享缓存和职责实例，连接消息路由，注册飞书事件并管理后台任务启停。
- `middleware/gateway_execution.py`：输入组装、批次执行、steer 转交及流式卡片交付。
- `middleware/gateway_runtime.py`：运行索引、提交锁、watchdog、停止确认、reaction 重试和过期清理。
- `middleware/gateway_commands.py`：撤回、`/stop`、`/new`、历史卡片及会话恢复。

`create_gateway()` 返回 runtime、execution、commands 和 router，四者共享一个 `SessionCache`。
外部服务通过构造参数传入；执行和命令组件依赖 runtime，业务模块不导入启动文件。
历史卡片由 commands 实例持有，启动层分别调度卡片清理、运行缓存清理、reaction 重试和 SDK 控制会话清理。

版本由 `setuptools-scm` 在构建时读取 Git 标签和提交状态，不再手写 `project.version`。
项目位于仓库子目录，因此启用向上查找 Git 根目录。建议发布标签使用 `v0.1.0` 等格式；
干净的标签提交使用该版本，标签之后的提交生成开发版本，未提交改动会带本地版本标识。
当前仓库没有标签，版本按工具的无标签规则计算；不会自动创建标签。

Docker 源码不包含 `.git`，没有可用 Git 或分发元数据时使用 `0.0.0`，表示版本未知。
需要对无 Git 的发布源码指定准确版本时，可在构建命令环境中设置
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_FERSK_CODEX`；普通开发构建不需要该变量。
`setuptools-scm` 仅作为构建依赖，不作为应用运行依赖。

配置加载入口为 `configs/loader.py`，工作区准备逻辑位于 `codex/codex_workspace.py`。
