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

主项目配置不再要求或校验 `mcp` 段，保留该字段只是兼容同一份挂载文件；扩展自行校验自己的配置。主项目默认配置不再包含 MCP 模型及服务设置。首次准备包含两个项目配置的文件时，可参考 `fersk_mcp/configs/config_default.json`；不要覆盖已有持久化配置。

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
