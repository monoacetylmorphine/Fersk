# 用户线程绑定

测试前请按根目录 README 安装包，并设置 `FERSK_CONFIG_FILE` 指向 `configs/config_default.json`。

`core/thread_manager.py` 使用 aiosqlite，在配置项 `storage.databasePath`
指向的数据库（当前为 `~/.fersk/state.sqlite`）中保存绑定。
首次访问自动创建目录和 `user_thread` 表，与 token usage 日志表共用数据库。

表字段为 `user_id TEXT PRIMARY KEY NOT NULL` 和可空的 `thread_id TEXT`。

- `await get_user_thread(user_id)`：读取绑定，用户不存在或已重置时返回 `None`。
- `await set_user_thread(user_id, thread_id)`：新增或更新绑定，提交后返回。
- `await set_user_thread(user_id, None)`：将线程 ID 设为 SQL NULL，持久化重置。

每次操作独立打开并关闭连接，写入采用参数化 SQL 和原子 upsert。
数据库异常向调用方传播，不回退到内存存储；SQLite 锁等待上限为 30 秒。
该模块不负责对整个 Codex 请求加锁，网关仅串行化输入组装及提交，回复流独立消费。

`codex.py` 每次请求读取绑定，在启动 turn 前保存线程 ID；即使 turn 启动失败，
下次请求仍可恢复该线程。新线程命令在原始 text 消息入口识别，确认旧任务停止并归档旧线程后持久化重置，
归档或数据库写入失败时返回错误，不报告重置成功。下一条普通消息才创建新 thread。
重启后保留已落库的绑定；旧版本内存字典中的绑定不会自动迁移。

在项目根目录运行测试：`python -m unittest discover -s tests -p 'test_thread_management.py'`。

## 中断当前 turn

网关的消息撤回和 `/stop` 共用 `FerskCodex.interrupt_and_confirm(run_id)`。
先发送 interrupt，再查询同一客户端的 thread 状态，只有 idle 才按正常路径确认停止。
启动中先记录待中断请求，阻止后续提交；无法在宽限时间内确认时关闭本次运行的
SDK 客户端，并检查它的 app-server 进程是否退出。未确认退出的会话禁止新任务，
可再次 `/stop` 重试。停止卡片只在确认后发送；未确认则明确提示未确认。
`/stop` 保留用户与 thread 的持久化绑定，不调用 `/new` 的归档或重置逻辑。

## 运行中追加输入（steer）

网关启动 `running(..., notify_started=True)` 并取得 `started` 事件后释放提交锁，
原请求继续消费唯一的回复流。后续输入调用 `FerskCodex.steer(run_id, prompt)`，
复用原 `AsyncThread` 和 `AsyncTurnHandle`；不会为 steer 创建独立 app-server。
客户端在本次运行结束时关闭，空闲后新请求按原有持久化绑定恢复 thread。

每次 steer 前调用 `thread.read()`，读取 `thread.status.root.type`：
`active` 调用 `handle.steer()`（SDK 自动携带 expectedTurnId）；`idle` 返回普通启动路径。
任务已退出、收到中断请求时也不 steer；加载异常、查询失败和未知状态返回错误。
服务端明确拒绝“无 active turn”或 turn ID 不匹配时，仅在再次查询确认 idle 后回退。
其余失败不自动重发，避免响应丢失造成重复执行。查询期间已撤回的输入不会提交。

状态查询/steer 与客户端关闭互斥，完成事件和提交响应并发时仍正确清理。
steer 成功的消息归属原 run，复用停止/撤回处理及一次用量日志；输出切换到新卡片。
网关为每个原 run 保存 `CardStreamSession`，通过控制通道暂停卡片写入后提交 steer。
只有明确接受才关闭旧卡、丢弃旧卡未发送缓冲，并立即发送“已收到补充”的新卡。
已显示的旧卡保留；同一个 thread、turn 和唯一回复流继续运行，后续正文替换新卡占位。
控制通道可以唤醒静默输出，最多预取一个模型事件，不取消正在进行的读取。
steer 失败或 idle 时恢复旧卡；换卡交付失败不重发已接受输入，也不主动中断模型。
结束事件与 steer 并发时先处理正在进行的换卡，再完成输出收尾；若没有后续正文，
新卡改为“补充已接收，当前任务已结束”。停止/撤回优先，禁止随后创建工作卡片。
steer 不能变更模型；含图片输入要求当前模型/provider 与配置的 image 路由一致，
否则提示等待结束或停止后重发。文本、文件、转写输入沿用当前模型。

离线回归：`python -B -m unittest discover -s tests -v`。
`test_steer.py` 包含真实 SDK handle 的串联测试，外部服务与数据库均模拟。

## 任务探针与超时

`core/thread_watchdog.py` 按 run_id 记录接收时间、阶段、thread/turn ID、消息归属、
最后活动、工具执行、停止原因及结果。终端只输出任务生命周期和最终 `item/completed` 的白名单摘要，
不输出 delta 或 item 中间事件；卡片持续展示推理和 commentary，最终答案替换当前卡正文，工具内容不展示。
卡片按 9 分钟关闭、后续文本到达时创建续卡。交付失败记录为 `delivery_failed`，
不会将模型任务改为失败或直接中断它；仍执行原有任务超时策略。
所有事件仍更新内存中的活跃时间和事件计数，工具开始/结束仍更新执行状态，
看门狗按原频率检查，但不再每秒写入 `probe` 记录。`lastEvent` 仅表示最近可记录事件。

`config.json` 的 `codex.watchdog` 配置（单位均为秒）：

| 配置 | 默认值 | 含义 |
| --- | ---: | --- |
| maxRunSeconds | 900 | 从接收消息计时，包含聚合、排队、准备、启动与执行 |
| startupTimeoutSeconds | 120 | Codex 启动阶段上限 |
| idleTimeoutSeconds | 0 | 0 关闭；启用后检查无事件时长，工具执行期间暂停 |
| checkIntervalSeconds | 1 | 独立看门狗检查间隔 |
| interruptGraceSeconds | 10 | interrupt 与 thread 状态确认的总宽限时间 |
| cleanupTimeoutSeconds | 5 | 单阶段资源清理上限 |
| cardRequestTimeoutSeconds | 10 | 飞书请求期限 |

steer 沿用原 run 的绝对期限，不自动续期。总期限不因工具执行而暂停。
超时触发允许一个检查间隔的误差；结束状态查询、中断及进程清理另有有界宽限。
在超时前额外查询对应 turn 的终态，避免卡片网络延迟造成已完成任务被误判。
只有 `turn/completed` 或已核对的服务端终态表示完成，异常 EOF 不算成功。

`storage.runLogPath` 配置运行日志目录，默认 `~/.fersk/logs`；相对路径以配置文件所在目录为基准，
也支持绝对路径，缺失的目录自动创建。按 `runtime.timezoneOffsetHours` 时区的入队日期
写入 `YYYY-MM-DD_logs.jsonl`，跨日无需重启，失败重试保留原日期。文件按行保存生命周期
和探针记录，每行一个 JSON 对象。
后台单写线程按批追加，保留已有记录，不再整文件重写或限制为最近 10000 条；通常在
100ms 后可见，终态也记录通知发送结果。流式内容只在 `item/completed` 时通过 `item`
字段保存与终端相同的完成项摘要，不记录 delta 或 `item/started`。摘要保留类型、ID、状态、耗时和退出码；
图片额外保留保存路径及透明背景标记，Agent 消息保留 phase 和字符数。
命令输出、图片 Base64、图片提示词、消息正文及其他工具结果均不写入摘要；
终端也不展开 turn 内嵌的 items。错误保留 message、additionalDetails 和 willRetry，
摘要中的字符串最多保留 500 字符，超出添加省略号（项目日志策略，非 SDK 限制）。
事件消费、卡片输出、用量保存及看门狗活动更新不受影响；历史日志不改写。
该文件按单网关进程设计，不支持多个网关同时写入；突然断电可能
丢失尚未落盘的记录或留下不完整的末行。旧 `log.json` 保留作为备份，不再写入。

当前进程确认适配已安装的 openai-codex 0.147.0，使用其内部进程引用检查退出，
升级 SDK 时应重新运行进程关闭测试。强制关闭覆盖当前 app-server；不声称撤销
已经完成的文件修改、远程工具操作或终止脱离父进程的后台终端。

看门狗离线测试：`python -B -m unittest discover -s tests -p 'test_watchdog.py' -v`。
测试使用模拟协议和本地短生命周期 Python 进程，不发送飞书消息，不调用模型。

## 配置启动校验

`utils/config_loader.py` 启动时使用本项目共享源 `configs/config_schema.json` 执行 Draft 2020-12 全量校验及格式校验，
拒绝非有限数值。直接与缓冲消息类型不得重叠，并集必须等于支持类型；两个命令不得相同
或带空白。错误包含配置路径、字段路径与约束名称，不打印配置值。自定义配置文件也需要
提供 `audio.limits.conversionTimeoutSeconds` 和 Schema 要求的消息文案。

两个服务完整校验同一文件；`maxRunSeconds` 必须为正整数。Git 初始化超时使用 `codex.gitInitTimeoutSeconds`，缺省为项目策略 10 秒。
