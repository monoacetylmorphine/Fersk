# Fersk MCP

## 启动

在项目根目录中，优先使用项目虚拟环境启动：

```bash
./.venv/bin/python server.py
```

也可以在安装为包后使用：

```bash
python -m fersk_mcp.server
# 或
fersk-mcp
```

直接执行 `server.py` 已兼容源码目录结构。

## Docker

构建镜像：

```bash
docker build -t fersk-mcp .
```

运行时需要提供已校验的配置文件。镜像支持 Compose 中的 `APP_USER`、`LOCAL_UID`、`LOCAL_GID` 和 `PYTHON_PACKAGE_INDEX` 构建参数；容器的 `HOME` 与 `CODEX_HOME` 会与相应的 `.fersk`、`.codex` 挂载目录对齐。以下命令将主机的 `~/.fersk` 挂载到默认容器用户的配置目录，并将默认 MCP HTTP 端口映射到主机：

```bash
docker run --rm \
  -p 8000:8000 \
  -e MCP_HOST=0.0.0.0 \
  -v "$HOME/.fersk:/home/app/.fersk:ro" \
  -v "$HOME/.codex:/home/app/.codex" \
  fersk-mcp
```

`config.json` 中的 `mcp.port`、`mcp.host` 与 `mcp.path` 可分别通过 `MCP_PORT`、`MCP_HOST`、`MCP_PATH` 环境变量覆盖。若配置文件不在默认位置，可设置 `FERSK_CONFIG_FILE`；该变量取值的来源由部署环境决定，Dockerfile 未预置任何凭据或配置内容。

综合部署使用仓库根目录 Compose，默认发布 `127.0.0.1:8000`，宿主机 URL 为 `http://127.0.0.1:8000/mcp`，同网络容器 URL 为 `http://fersk-mcp:8000/mcp`。由用户通过 Codex CLI 自行配置连接。Compose 的 `MCP_PORT` 同时控制宿主机映射和容器监听端口，`MCP_BIND_ADDRESS` 控制宿主机绑定地址；通过 shell 或 `--env-file` 设置。镜像通过 `uv sync --locked --no-dev --no-editable` 安装锁定依赖，并包含 `fersk_mcp.utils`。

## 工具解耦

服务启动仅加载共享配置和注册工具，不校验各工具的 API 凭据。图像工具在每次调用时校验 `mcp.imageModel` 及其环境变量，并通过异步上下文关闭客户端；缺少图像配置或调用失败不会阻止文件工具使用。飞书客户端同样仅在调用文件工具时初始化，缺少飞书凭据不影响图像工具。

环境变量统一从 `~/.fersk/.env` 加载，模块不再搜索或覆盖其他 `.env`。修改挂载配置后重启服务生效。JSON 语法和共享 Schema 错误仍会阻止启动。

离线验证：从仓库根目录运行 `fersk_mcp/.venv/bin/python -B fersk_mcp/tests/test_runtime.py`，测试使用临时配置和模拟客户端，不发送真实请求。

独立配置示例位于本项目 `configs/config_default.json`，测试不再读取主项目文件。已有挂载的配置保持不变。
