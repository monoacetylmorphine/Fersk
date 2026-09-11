# Fersk Codex

主项目通过飞书消息驱动 Codex，持久化配置与工作区从宿主机 `~/.fersk`、`~/.codex` 挂载读取。部署方式见[根目录说明](../README.md)。

主项目直接使用 `AsyncCodex()`，由你通过 Codex CLI 管理挂载的用户配置；代码不注册扩展或覆盖其连接设置。两个服务共用本项目 `configs/` 下的 `config_default.json`、`config_schema.json`、`config_validation.py` 和 `initialize_config.py`，启动时完整校验，主项目不消费 `mcp` 段。共享源仍归属于本项目，MCP 使用符号链接引用。

群聊机器人身份使用 `lark.credentials.robotUnionIdEnv` 指定环境变量名，默认读取 `LARK_ROBOT_UNION_ID`，并与飞书消息中的 `mention.id.union_id` 匹配；`robotNameEnv` 可作为名称匹配后备。

在已安装锁定依赖的环境中使用 `python -m fersk_codex.gateway` 启动。离线测试从本项目目录运行 `python -B tests/run_tests.py`。

工作区只在缺少 `.git`（目录或 worktree 文件）时初始化仓库。Git 初始化采用异步子进程，超时取 `codex.gitInitTimeoutSeconds`（默认 10 秒），取消或超时后回收子进程；初始化失败不提交模型任务。附件响应读取、校验和文件写入移至工作线程。用量仅写入 SQLite，并按任务回填耗时，不再生成 CSV。

Docker 从仓库根目录执行 `docker build -f fersk_codex/Dockerfile -t fersk-codex .`。容器首次启动原子初始化共享挂载配置，已有配置不覆盖。
