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

从仓库根目录构建镜像：

```bash
docker build -f fersk_mcp/Dockerfile -t fersk-mcp .
```

首次启动会从镜像中的共享默认配置原子初始化挂载的 `~/.fersk/config.json`，已有文件不覆盖。两个服务均完整校验共享配置；指定自定义配置路径时，文件必须已存在。镜像支持 Compose 中的 `APP_USER`、`LOCAL_UID`、`LOCAL_GID` 和 `PYTHON_PACKAGE_INDEX` 构建参数；容器的 `HOME` 与 `CODEX_HOME` 会与相应的 `.fersk`、`.codex` 挂载目录对齐。以下命令将主机的 `~/.fersk` 挂载到默认容器用户的配置目录，并将默认 MCP HTTP 端口映射到主机：

```bash
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -e MCP_HOST=0.0.0.0 \
  -v "$HOME/.fersk:/home/app/.fersk" \
  -v "$HOME/.codex:/home/app/.codex" \
  fersk-mcp
```

`config.json` 中的 `mcp.port`、`mcp.host` 与 `mcp.path` 可分别通过 `MCP_PORT`、`MCP_HOST`、`MCP_PATH` 环境变量覆盖。若配置文件不在默认位置，可设置 `FERSK_CONFIG_FILE`；该变量取值的来源由部署环境决定，Dockerfile 未预置任何凭据或配置内容。

综合部署使用仓库根目录 Compose，默认发布 `127.0.0.1:8000`，宿主机 URL 为 `http://127.0.0.1:8000/mcp`，同网络容器 URL 为 `http://fersk-mcp:8000/mcp`。由用户通过 Codex CLI 自行配置连接。Compose 的 `MCP_PORT` 同时控制宿主机映射和容器监听端口，`MCP_BIND_ADDRESS` 控制宿主机绑定地址；通过 shell 或 `--env-file` 设置。镜像通过 `uv sync --locked --no-dev --no-editable` 安装锁定依赖，并使用 `UV_NO_CACHE=1` 避免保留 uv 缓存；包含 `fersk_mcp.utils`。独立运行示例仅将无鉴权 MCP 端口发布到宿主机回环地址。

## 工具解耦

飞书上传和发送使用本进程独立的有界执行器，`lark.requestConcurrency` 缺省 8；来源为内部最多
5 个并发任务的初始保护策略，尚非压测结论。满额立即报错，实际线程结束才归还名额，调用方
超时或取消不会增加可提交容量。SDK timeout 与调用方 timeout 均使用
`codex.watchdog.cardRequestTimeoutSeconds`。上传文件由 worker 打开和关闭，避免超时后提前关闭句柄。
该方案不能强制终止永久阻塞的线程，不自动重试结果不确定的发送。

业务 logger 使用 `mcp.logLevel`，不再固定 INFO；飞书 SDK 仍保持 DEBUG，仅遮蔽连接 URL 凭据。
上述新增容量字段可省略，兼容旧配置。两个独立安装的服务各自包含标准库执行器实现，回归测试同时验证两份实现。

服务启动仅加载共享配置和注册工具，不校验各工具的 API 凭据。图像工具在每次调用时校验 `mcp.imageModel` 及其环境变量，并通过异步上下文关闭客户端；缺少图像配置或调用失败不会阻止文件工具使用。飞书客户端同样仅在调用文件工具时初始化，缺少飞书凭据不影响图像工具。

环境变量统一从 `~/.fersk/.env` 加载，模块不再搜索或覆盖其他 `.env`。修改挂载配置后重启服务生效。JSON 语法和共享 Schema 错误仍会阻止启动。

共享配置中的机器人身份字段统一为 `lark.credentials.robotUnionIdEnv`，其默认环境变量为 `LARK_ROBOT_UNION_ID`；旧字段 `robotOpenIdEnv` 不再通过 Schema 校验。

离线验证：从仓库根目录运行 `fersk_mcp/.venv/bin/python -B fersk_mcp/tests/test_runtime.py`，测试使用临时配置和模拟客户端，不发送真实请求。

默认配置、Schema、校验逻辑和初始化脚本的唯一实体位于 `fersk_codex/configs/`；本项目内同名路径是指向它们的符号链接，校验与初始化模块分别为 `configs/validation.py` 和 `configs/initialization.py`。两个服务执行相同的完整校验后各自使用所需字段，详情见根目录 README。已有挂载配置不自动覆盖。

`configs/loader.py` 通过符号链接共用 `fersk_codex/configs/loader.py`，MCP 保留存储路径原值，Codex 将其解析为绝对路径；模块分别加载，配置对象相互独立。

`services/lark/lark_requests.py` 通过符号链接共用 `fersk_codex/services/lark/lark_requests.py`，两个服务仍各自加载配置并创建独立线程池。Docker 构建会一并复制共享源码。`services/lark/lark_client.py` 保持独立，以保留 MCP 调用工具时才校验飞书凭据的行为；Codex 则在启动时校验凭据并提供 WebSocket 客户端。

文件发送的接收方目录必须同时满足正则 `(?:on_|oc_)[A-Za-z0-9]+` 与总长度 35，并且只识别到一个接收方。长度取自用户提供的参考 ID（3 字符前缀加 32 字符主体）；不新增调用授权或工作区归属检查。
