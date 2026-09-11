# Fersk-dev

`fersk_codex` 为主项目，`fersk_mcp` 为 MCP 扩展；根目录 `docker-compose.yaml` 编排两个项目与 Langfuse。

## 持久化配置

两个项目都挂载宿主机 `~/.codex` 和 `~/.fersk`，容器中对应 `/home/app/.codex` 与 `/home/app/.fersk`；自定义 `APP_USER` 时路径随用户名调整。

- `~/.fersk/config.json`：共享业务配置，包含 MCP 端口、路径、模型和持久化数据路径。
- `~/.fersk/.env`：应用凭据；只由统一配置加载器读取，已有进程环境变量优先。
- `~/.codex`：Codex 配置、认证、插件与工作区；`langfuse.json` 继续使用原有 Web 地址。

不自动覆盖挂载的配置。`HOST_CODEX_DIR`、`HOST_FERSK_DIR` 可改变宿主机挂载来源；默认目录必须已存在。Compose 的 `${...}` 插值使用 shell 或 `--env-file`，不会自动读取应用加载的 `~/.fersk/.env`。

## 容器通信

两个项目独立启动，使用已存在的外部网络 `ai-infra`，没有服务间启动依赖或自动注册逻辑。主项目直接使用 `AsyncCodex()`，由你通过 Codex CLI 管理挂载目录中的配置。

MCP 在容器内监听 `0.0.0.0`，Compose 默认发布到宿主机 `127.0.0.1:8000`。默认路径 `/mcp` 下：

- 宿主机 Codex CLI：`http://127.0.0.1:8000/mcp`。
- 同一 `ai-infra` 网络的 Codex 容器：`http://fersk-mcp:8000/mcp`。

请按 CLI 实际运行位置选择地址；容器中的 `127.0.0.1` 不指向宿主机。这里只提供地址，不自动调用 CLI 或改写其配置。

`MCP_PORT` 是 Compose 的宿主机端口及容器监听端口，默认 8000；用 shell 或 `--env-file` 设置，并同步注入 MCP 容器，避免映射不匹配。`MCP_BIND_ADDRESS` 默认 `127.0.0.1`；需要远程访问时可显式设置可访问的宿主机地址。服务当前无鉴权，保持默认本机绑定可避免直接开放到局域网。`mcp.path` 继续来自共享 `config.json`，也可由共享 `.env` 中的 `MCP_PATH` 覆盖。

四个共享文件的唯一实体均在 `fersk_codex/configs/`：`config_default.json`、`config_schema.json`、`config_validation.py`、`initialize_config.py`。MCP 的 `configs/` 下同名文件通过符号链接引用；请在完整仓库中开发和构建，保留符号链接，不要单独拷贝项目目录。

两个服务启动均完整校验同一份 `~/.fersk/config.json`，任何配置段格式错误都会阻止启动。校验通过后各自使用所需字段：Codex 不初始化 MCP 服务或消费其模型设置；MCP 使用 `mcp`、飞书凭据/上传设置和共享请求超时，不启动 Codex。校验不读取模型密钥值，缺少图像凭据仅在调用图像工具时报告。

Docker 构建上下文为仓库根目录，镜像内置同一份默认配置。挂载发生在容器启动时，因此两个入口脚本在启动阶段将默认文件原子复制并命名为 `/home/${APP_USER:-app}/.fersk/config.json`。并发首次启动只发布一个完整文件（权限 0600）；已有文件不覆盖。指定 `FERSK_CONFIG_FILE` 时对应文件必须已存在。

`codex.watchdog.maxRunSeconds` 必须为正整数。新增 `codex.gitInitTimeoutSeconds` 默认 10 秒，旧配置缺省时仍使用 10 秒；此值为项目策略。旧 `storage.tokenUsagePath` 仅为挂载兼容保留，不再消费或生成 CSV，历史 CSV 不删除。其他不再符合完整 Schema 的旧配置会明确报错，需要按字段调整；不会自动覆盖或迁移挂载文件。

群聊继续按 `oc_` 群 ID 共享聊天历史、Codex 线程和工作空间。MCP 文件发送要求完整目录名同时满足 `(?:on_|oc_)[A-Za-z0-9]+` 和长度等于 35（含前缀），且接收方唯一；长度来自用户提供的参考 ID。授权与归属校验暂未增加。

## Langfuse

Langfuse 相关服务仅发布 Web `3000`，地址保持 `http://langfuse-web.observability.orb.local:3000`，`NEXTAUTH_URL` 与其一致。Worker、MinIO 控制台、S3、数据库及 Redis 均不发布宿主机端口。

仅 Web 模式不配置可选的 S3 媒体上传，因此不提供浏览器直传／读取 MinIO 媒体附件；事件存储仍走 `http://minio:9000`。文本追踪和 Web 界面保留。媒体预签名链接要求浏览器可达的存储入口，不能用 Web 地址直接替换 S3 Endpoint，参见 [Langfuse 存储文档](https://langfuse.com/self-hosting/deployment/infrastructure/blobstorage)。现有持久化数据不会被删除。

## 构建与验证

两个项目均使用各自 `uv.lock` 安装依赖。按部署要求，Langfuse、Worker、PostgreSQL、ClickHouse、MinIO 和 Redis 均使用 `latest`；Python 使用 `3.13.15-slim-bookworm`，主项目 Node 使用最新 `lts-bookworm-slim`，两个项目 uv 使用 `latest`。基础设施镜像、Node、uv 与系统包仍随构建或拉取更新，未宣称逐字节可复现构建。

```sh
docker compose config --quiet
docker compose build fersk-codex fersk-mcp
fersk_codex/.venv/bin/python -B fersk_codex/tests/run_tests.py
fersk_mcp/.venv/bin/python -B fersk_mcp/tests/test_runtime.py
```

应用配置或镜像更新需要重建／重启后生效。当前已有单独 Compose 部署时，不要直接启动第二套同名服务；先核对现有项目与数据卷，再安排切换。
