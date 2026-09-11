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

两个服务均从仓库根目录构建，统一使用根目录 `.dockerignore`，子项目不再维护独立忽略文件。Codex 使用源码与构建输入白名单（包含 MCP 依赖的共享配置和入口脚本），MCP 保留目录内容；最后统一排除 `.venv`、`__pycache__`、`*.egg-info`、`build`、`dist`、`.env`、`.git`、`tests` 和 `.DS_Store`。新增 Codex 源码目录或非 Python 资源时，需要同步更新根目录白名单。

两个项目均使用各自 `uv.lock` 安装依赖。按部署要求，Langfuse、Worker、PostgreSQL、ClickHouse、MinIO 和 Redis 均使用 `latest`；Python 使用 `3.13.15-slim-bookworm`，主项目 Node 使用最新 `lts-bookworm-slim`，两个项目 uv 使用 `latest`。基础设施镜像、Node、uv 与系统包仍随构建或拉取更新，未宣称逐字节可复现构建。

```sh
docker compose config --quiet
docker compose build fersk-codex fersk-mcp
fersk_codex/.venv/bin/python -B fersk_codex/tests/run_tests.py
fersk_mcp/.venv/bin/python -B fersk_mcp/tests/test_runtime.py
```

应用配置或镜像更新需要重建／重启后生效。当前已有单独 Compose 部署时，不要直接启动第二套同名服务；先核对现有项目与数据卷，再安排切换。

## Runtime：从飞书消息接收到 reaction 删除

正常对话的主流程：

**飞书推送消息 → 入口限流 → 过滤去重 → 添加 reaction → 路由与历史组装 → 创建运行状态和 watchdog → 准备输入 → 启动或追加 Codex 任务 → 流式卡片交付 → 模型完成 → 关闭卡片与事件流 → 删除 reaction → 释放任务。**

以下流程根据当前源码整理，数值均为默认配置，实际运行以加载的配置为准。8 个飞书请求名额、32 个普通事件名额、4 个控制事件名额和 30 秒收尾期限，来源于内部 30 人以内、最多 5 个任务并发的初始保护策略，尚非压测最优值。

### 1. 飞书 WebSocket 接收到消息

入口为 [gateway.py](fersk_codex/gateway.py) 的 `main()`。启动时初始化业务日志、事件处理器和缓存维护任务，通过 `asyncio.to_thread(websocket_client.start)` 启动飞书长连接。

收到 `im.message.receive_v1` 后，SDK 调用同步回调 `do_p2_im_message_receive_v1()`。回调判断是否为 `/stop`、`/new`，再通过 [EventDispatcher.submit()](fersk_codex/utils/event_dispatcher.py) 把处理协程提交到主事件循环。

普通事件默认最多 32 个在途处理；控制事件独立预留 4 个名额。容量不足时尝试发送繁忙提示，提示自身最多一个在途。事件 Future 的异常会被消费并记录。

### 2. 检查消息是否需要处理

进入 [message_router.py](fersk_codex/middleware/message_router.py) 的 `processing()` → `_processing()`：

- 私聊消息继续处理；群聊要求当前消息提及机器人。
- 已接收过的 `message_id` 直接跳过。
- 记录接收时间，用于后续计算运行期限。
- `/stop`、`/new` 转入命令分支，不进入模型输入。
- 普通消息检查会话是否正在重置，以及 `generation` 是否仍有效。

`generation` 是会话版本号：停止或重置会推进版本；旧消息处理过程中发现版本变化，就停止继续提交。入站处理外层还有 24 小时期限，会话引用通过 `cache.hold()` 管理。

### 3. 给用户消息添加“处理中”reaction

普通消息的调用链为：

`adding_reaction_emoji()` → `call_lark()` → `BoundedExecutor.call()` → 飞书 `message_reaction.create`。

添加成功后，保存 `chat_id → message_id → reaction_id`，后续使用 `reaction_id` 删除表情。添加失败只记日志，仍继续处理用户任务。

[飞书请求入口](fersk_codex/services/lark/lark_requests.py)使用[专用执行器](fersk_codex/utils/bounded_executor.py)，默认每个服务进程最多 8 个实际在途请求，单次调用默认 10 秒期限。容量满时立即失败，不继续堆积请求。调用方超时或取消后，已经开始的同步线程仍占用名额，直到真正结束；该机制不能强制终止永久阻塞的线程。

### 4. 根据消息类型立即处理或等待缓冲

`_route_message()` 按配置分流：

| 消息类型 | 默认行为 |
| --- | --- |
| 文本、富文本、语音 | 立即处理，并取消该会话尚未结束的附件缓冲计时 |
| 图片、文件 | 进入固定 10 秒缓冲窗口 |
| 不支持的类型 | 发送提示，清理该消息的 reaction |

缓冲窗口内的新消息不会延长计时。缓存保存最新触发事件，窗口结束后通过飞书历史接口收集消息，而不是直接把缓存事件逐条拼接。直接消息到达时也通过历史收集吸收符合条件的先前附件。

### 5. 拉取历史并形成消息批次

`_process_chat_history()` 获取最近默认 10 条历史消息，再由 [message_collector.py](fersk_codex/middleware/message_collector.py) 的 `batch_from_chat_history()` 生成 `MessageBatch`：

- 以本次触发消息为边界，避免提前吞入之后才到达的新输入。
- 从新向旧扫描，遇到最近的应用回复、`/new` 或 `/stop` 就停止。
- 保留未删除、受支持的用户消息，再恢复为时间正序。
- 历史接口尚未出现当前消息时，用接收事件补入当前消息。

私聊的回复目标和工作区标识使用 `union_id`；群聊使用 `chat_id`，按群共享会话与工作区。历史请求失败时发送失败提示并尝试清理 reaction，不提交模型任务。

### 6. 创建运行状态，准备模型输入

批次进入 `gateway.py` 的 `_handle_message_batch()`。先检查容量、会话阻塞状态，并过滤已经处理或正在处理的消息；随后创建 `ActiveCodexRun`、唯一 `run_id` 和 `RunProbe`，同时启动执行任务与 watchdog。

执行任务 `_execute_message_batch()` 取得该会话的 `_submission_lock`，再次检查消息状态，登记活动消息归属，进入 `preparing` 阶段，然后调用 [assemble_codex_input()](fersk_codex/middleware/message_assemble.py)：

- 文本转换为 `TextInput`。
- 图片下载并校验后转换为 `LocalImageInput`。
- 文件下载并校验后转换为带本地路径的 `MentionInput`。
- 语音经检测、必要的转码和 ASR 后转换为 `TextInput`。
- 每批附件总量默认不超过 10 个，与 `messaging.historyPageSize` 共用上限。
- 部分附件失败时提示并处理其余可用输入；附件全部不可用时，整批附件及附带文本都不提交。

解析错误、空输入或附件超限会发送对应提示，随后进入收尾。

### 7. 决定追加旧任务，还是启动新任务

输入准备完成后查询 `active_runs_by_chat`。如果会话已有 owner，优先尝试 `FerskCodex.steer()`：

- 追加成功：把新消息归属转交给旧 owner，协调卡片切换；后续输出和这些消息的 reaction 清理由旧 owner 负责。
- 旧模型已经结束：等待旧 owner 的 `finished`，再检查会话状态并尝试启动新任务。
- 追加出错：提示错误并结束当前提交。

没有可追加的 owner 时，进入 `starting`，调用 [FerskCodex.running()](fersk_codex/core/codex.py)：

1. 从数据库读取用户或群对应的 thread 绑定。
2. 根据文本、文件或图片输入选择模型配置。
3. 准备工作区和 Codex 客户端。
4. 尝试恢复已有 thread；按错误类型尝试解除归档或新建。
5. 保存 thread 绑定，再调用 `thread.turn(input=prompt)`。
6. 返回内部 `started` 状态。

gateway 收到 `started` 后创建 `CardStreamSession`，登记会话 owner，进入 `running`，随后释放提交锁。模型输出和卡片更新期间不一直持有提交锁。

### 8. 边消费模型事件，边更新飞书卡片

输出链为：

`FerskCodex.running()` → `CardStreamSession.events()` → `_reply_content()` → `sending_card()`。

模型事件会更新 probe 活动时间，并按类型转换：

- 推理和 commentary 作为过程内容展示。
- 最终答案首段通过 `CardReplace` 替换之前的正文，后续片段继续追加。
- 工具事件主要用于状态和日志，不直接作为卡片正文。
- 用量事件写入数据库；当时尚未获得最终耗时，日志中的 `taskDuration_ms` 可以是 `0`，结束时再回填。

[lark_card.py](fersk_codex/services/lark/lark_card.py) 在首次需要展示内容时创建 CardKit 卡片，再发送引用该卡片的飞书消息；之后持续更新正文，普通更新按约 0.25 秒间隔合并，实际也受网络耗时影响。单张流式卡片约 9 分钟后关闭，后续内容按需续卡。

卡片交付失败时记录错误并继续消费模型事件，不直接打断健康的模型任务，也不自动重发结果不确定的请求。

### 9. 模型完成，结束输出和清理

收到 `turn/completed` 后，probe 记录模型结果并开始独立收尾计时；此时还没有完成整个任务释放。

后续正常流程包括：

- 结束模型事件消费，清理 live turn 状态，回填用量耗时。
- 将剩余卡片内容刷新到飞书。
- 调用 CardKit settings 关闭 `streaming_mode`，产生“流式卡片已关闭”日志。
- 关闭事件生成器及相关客户端资源。
- 进入 gateway 的 `finally` 收尾。

这些输出和源流关闭操作存在嵌套关系，并非每项都有独立终端日志。

### 10. 删除 reaction，再完成任务释放

正常收尾先移除本任务拥有的活动消息索引、会话 owner 索引，并调用 `FerskCodex.forget_run()` 清理运行记录，然后对未转交给其他 owner 的消息调用 `_clear_reaction()`。

删除调用链为：

`_clear_reaction()` → [delete_reaction_emoji()](fersk_codex/services/lark/lark_tools.py) → `call_lark()` → 专用执行器 → 飞书 `message_reaction.delete`。

`_clear_reaction()` 检查归属和正在清理的标记，防止并发重复删除：

- 成功：移除本地 reaction 记录。
- 请求异常：记录完整堆栈，继续后续释放。
- 当前没有网络恢复后补删机制，远端表情可能遗留；最终任务释放也会清除对应本地记录。

最后设置 `state.finished`，外层 `_release_run()` 幂等清理剩余运行索引和缓存，并记录类似日志：

```text
任务已释放: run_id=..., terminal=completed, cleanup_timeout=False
```

飞书随后推送的 `im.message.reaction.deleted_v1` 是删除结果事件通知，当前 handler 直接忽略；它不是任务释放的前置条件。

### 贯穿流程的异常保护与特殊分支

[watchdog](fersk_codex/core/thread_watchdog.py) 与执行任务并行运行，默认每秒检查一次：

| 检查项 | 配置字段 | 默认值 |
| --- | --- | --- |
| 运行硬期限，基于接收时间计算 | `codex.watchdog.maxRunSeconds` | 900 秒 |
| 启动阶段期限 | `codex.watchdog.startupTimeoutSeconds` | 120 秒 |
| 空闲超时 | `codex.watchdog.idleTimeoutSeconds` | 0，关闭 |
| 模型终态或停止后的收尾期限 | `codex.watchdog.finalizationTimeoutSeconds` | 30 秒 |
| 收尾超时后的关闭宽限 | `codex.watchdog.cleanupTimeoutSeconds` | 5 秒 |
| 飞书单次请求期限 | `codex.watchdog.cardRequestTimeoutSeconds` | 10 秒 |

超时触发停止或有界清理。若残留 worker 未结束、进程退出未确认，则唤醒等待者并隔离会话，避免新旧任务重叠。这种异常释放路径不保证远端 reaction 已经删除。后台还会维护 24 小时缓存保留期限；事件循环本身阻塞时不承诺准点超时。

`/stop` 和撤回走中断及 reaction 清理分支；`/new` 先停止旧任务，再重置 thread 绑定。“进入机器人单聊”、已读及 reaction 创建／删除通知注册了空处理器，不进入模型流程。

飞书 SDK 的 HTTP 和 WebSocket 日志均保持 DEBUG，连接 URL 凭据会被脱敏。Codex 业务日志使用 `logging.logLevel`，MCP 业务日志使用 `mcp.logLevel`。

`fersk_mcp` 只在模型实际调用相应 MCP 工具时参与，例如文件上传发送；普通文字对话从接收到 reaction 删除，并不必然经过 MCP 服务。MCP 文件发送使用该服务自己的有界执行器。
