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

## 历史会话与首次命名

`core/session_history.py` 在同一个 `storage.databasePath` 中自动创建 `session_history` 表，
不修改 `user_thread` 表结构。首次访问只创建新表及索引，不迁移或删除已有绑定。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | TEXT NOT NULL | 沿用活跃绑定的身份范围：私聊用户 union_id，群聊 chat_id |
| `thread_id` | TEXT NOT NULL | Codex 线程 ID，与 user_id 组成主键 |
| `thread_name` | TEXT NULL | 首次文本生成的名称；无文本时为空 |
| `updated_at` | INTEGER NULL | 最近同步的 SDK Unix 秒级时间；初始化未完成时可为空 |

名称取首次提交文本，合并换行和连续空白，保留前 15 个 Unicode 字素簇，超长追加 `…`。
使用 `regex` 的 `\X`，不按语言分支，不拆开组合重音、组合文字与 emoji。
输入列表按顺序提取 TextInput（包括语音转写），忽略附件路径；原始模型输入不截断。
纯附件线程在首次收到文本时命名，包括运行中的 steer 输入。已有名称不会被后续提示词替换。

新线程在提交 turn 前登记历史、保存活跃绑定并完成 SDK 命名。名称候选先落库，
命名或元数据写入失败则报错并保留候选；恢复时读取远端名称，若已一致则不重复 set_name。
`thread_name` 非空且 `updated_at` 为空表示命名同步待完成，时间同步不会越过这个状态。
SQLite 与 SDK 无共同事务，不保证故障时网络调用严格只发生一次。
已有旧版本绑定不自动回填历史，避免将当前消息当作旧线程的首次提示词。

每次收到 turn/completed（成功、失败或中断）后，以及历史恢复时，读取 SDK 的 updated_at
更新原记录；名称不改、记录不重复。时间更新不回退，不用本地时钟伪造 SDK 时间。
列表按 updated_at 降序，同秒按 thread_id 降序稳定排序，空时间排在末尾。
排序反映 SDK 最后更新时间；若 SDK 解除归档未推进时间，恢复操作也不会人为置顶。
断流、进程强制退出等未观察到终态的情况不保证同步最新时间；同步失败记错误日志，
保留旧时间，不重发已执行输入、不改变已确定的模型执行结果。

`/new` 仍归档并清空活跃绑定，历史记录保留；下一条普通消息创建新的历史记录。

## 前端历史选择与恢复接口

私聊 `/history` 已接入历史选择卡片和 `card.action.trigger` 回调，交互卡片单独位于
`services/lark/lark_interactive_card.py`，普通通知及流式卡片继续由 `lark_card.py` 负责。
以下是网关复用的异步 Python 入口，不是 HTTP 路由：

```python
from fersk_codex.gateway import history_options, processing_history_restore

options = await history_options(data)
# [{"label": "きさらぎ駅是什么", "value": "thread-id", "updated_at": 1789637171}]
result = await processing_history_restore(data, selected_thread_id)
# 成功：{"ok": True, "thread_id": "thread-id", "content": "已恢复历史会话"}
# 失败：{"ok": False, "content": "恢复历史会话失败"}
```

`data` 使用现有已验证消息事件的身份结构，前端回调适配层必须从认证信息构建，
不能接受客户端任意传入的 user_id 或群聊 chat_id。选项显示 label（可附带时间），
提交 value，不能用名称或时间反查线程。同名和同秒记录均允许存在。
列表查询错误向调用方传播，适配层应展示加载失败，不能伪装成空列表。

### 私聊 `/history` 交互

1. 在机器人私聊中发送独立文本 `/history`（忽略首尾空白和大小写）。
2. 下拉菜单展示 thread_name，空名称显示“未命名会话”，value 为 thread_id。
3. 点击“激活此会话”，再在二次确认弹窗中选择确认或取消。
4. 取消不提交表单，不停止任务、不解归档、不写入数据库；下拉选中的显示值可能保留。
5. 确认后先提示正在处理，完成恢复和写库后原卡片显示“已激活”；失败明确提示。

采用 Card JSON 2.0 表单的 `form_action_type: submit` 按钮搭配 `confirm`，不依赖下拉组件直接触发弹窗。
下拉本身没有 callback；最终 thread_id 从 `action.form_value.history_thread` 读取，
仅接受 `activate_history` 提交按钮，拒绝 `action.option` 或单纯选择事件触发恢复。
参考：[表单容器](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/containers/form-container)、
[按钮](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/interactive-components/button)。

卡片默认只提供最近 30 个会话，在同一下拉菜单中展示，不再通过翻页访问更早记录。
通过 `messaging.sessionHistoryLimit` 设置选项总数上限，要求正整数；默认配置及 Schema 默认值均为 30，
旧配置缺少此字段时也按 30 处理。修改配置后重启网关生效。
按 `session_history.updated_at` 降序排序后截取（数据库实际字段名是 `updated_at`）；
同秒按 `thread_id` 降序稳定排序，空时间排在最后，不足上限时全部展示。
该限制只影响私聊卡片，不删除历史数据，不修改历史写入或通用查询接口。
这是用户指定的展示策略，非平台上限；与飞书聊天消息拉取的 `messaging.historyPageSize` 无关。
卡片选项使用发送时的历史快照，
重新 `/history` 获取最新列表。所有权在真正恢复前再次查询数据库，历史删除或归属不符时拒绝。
仅私聊入口使用个人 union_id；群聊没有历史选择入口，群聊 session_history 的记录和原有消息处理不变。
私聊 `/history` 不进入模型，并作为后续聊天历史收集边界；群聊不应用该过滤规则。
`/history` 为固定命令，不新增配置字段；既有自定义 `/new`、`/stop` 命令不应设为 `/history`，
私聊入口中 `/history` 优先。

服务端为每张卡保存随机 ticket、操作者 union_id、原私聊 chat_id、message_id 及选项快照。
回调同时核对 operator.union_id、context.open_chat_id、context.open_message_id，拒绝转发到群聊、
其他用户操作和伪造卡片参数。每张卡确认只消费一次；翻页使用 revision 拒绝旧页重复回调。
处理失败后重新发送 `/history`，避免同一回调重投造成重复停止或恢复。
状态仅保存于当前网关进程，24 小时失效，最多保存 1024 张，满时淘汰最早的非处理中卡片；
这些值来源于本功能初始内存策略，非压测结论。重启或淘汰后旧卡片提示重新 `/history`。
该机制沿用项目的单网关部署方式，不支持多进程之间共享卡片状态。

SDK 同步回调通过 EventDispatcher 的独立控制容量提交给主事件循环，不等待 SDK 解归档或数据库操作。
“正在处理”不是成功承诺；原卡更新失败时尝试另发结果卡，不再次执行恢复。
若结果发送也失败，仅记录交付异常，不能撤销已经提交的活跃绑定。

部署时需在飞书应用后台启用新版 `card.action.trigger` 回调并使用当前长连接接收方式，
继续使用现有应用凭据和消息权限，不需要新增模板 ID 或明文凭据。
离线测试使用真实 SDK 请求/回调模型，但不调用真实飞书或 Codex。
飞书客户端实际表单渲染、确认/取消行为和后台回调投递仍需部署联调验证。
专项回归：`python -B tests/run_tests.py --pattern test_lark_interactive_card.py`；
恢复和持久化回归：`python -B tests/run_tests.py --pattern test_session_history.py`。

恢复先验证历史记录归属。选择当前活跃线程直接成功，不停止任务或重复解除归档。
切换其他线程时，复用 reset_tasks 门禁、停止确认、任务退出等待和提交锁；新输入等待
切换完成。停止未确认时禁止切换。之后直接调用 SDK thread_unarchive，读取元数据，
SDK 操作与客户端关闭成功后同步时间，最后写入原活跃映射。
不额外归档此前的活跃线程，不重新命名已完成命名的目标，也不创建替代线程。
选中未归档目标时仍按约定直接调用 thread_unarchive；SDK 若拒绝则返回恢复失败。

任一步失败返回统一错误，保留原映射。解除归档成功后若数据库失败，目标可能已经
解除归档，不承诺回滚 SDK 状态。底层 `FerskCodex.restore_session(user_id, thread_id)`
仅供已完成停止与锁保护的调用方使用；前端应使用网关入口。
并发保证沿用现有单网关进程、按 chat_id 串行提交的部署模型，不支持多个网关共同切换绑定。

离线专项测试：`python -B tests/run_tests.py --pattern test_session_history.py`。

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

## 内存状态释放与 24 小时保留期限

正常结束、取消、超时和失败后释放本任务的活动索引、时间戳、reaction 元数据、卡片及任务引用。
会话没有待处理输入、缓冲、重置或活动任务时，回收锁和 generation；等待中的新消息继续持有会话，
避免旧任务收尾删除新状态。steer 已转交的消息随接收它的原任务清理。数据库 thread 绑定、工作区和已写日志保留。

轻量接收/处理 ID 最长保留 24 小时以防重复执行；停止未确认的阻塞元数据也最多保留 24 小时。
后台按 `checkIntervalSeconds` 清扫，即使没有新消息也执行。过期任务取消后做有时限的进程关闭尝试，
即使关闭失败也清除索引并告警；此时不能把清理缓存等同于确认底层进程停止。任务总期限同时受 24 小时硬上限约束。
到期检查允许一个扫描间隔，关闭另有 `cleanupTimeoutSeconds` 宽限；事件循环被阻塞时无法保证准点清理。

JSONL 每批追加并 flush 成功后立即从内存队列移除；失败保留原记录重试。
每条未写记录从入队起最多保留 24 小时，到期丢弃并输出明确的未落盘告警；退出 flush 不会把丢弃报告为写入成功。
该期限只清理内存，不删除已写入的 JSONL 文件。后台写入正常调度时检查到期；系统磁盘调用永久阻塞时无法保证及时释放。

## 模型终态后的收尾保护

`turn/completed` 仅确定模型结果，watchdog 持续监督至任务释放。模型终态或停止请求开始独立的
`codex.watchdog.finalizationTimeoutSeconds` 计时，缺省 30 秒；该值来源于内部最多 5 个并发任务的
初始运维策略，尚非压测结论。超时后取消输出，另外给予 `cleanupTimeoutSeconds` 的强制关闭预算，
随后以无网络等待的幂等操作移除活动索引、唤醒 `finished` 等待者并记录 `cleanup_timeout` / `released`。
超时提示单独受卡片请求期限约束；模型已经 `completed` 时不会改写成模型执行失败。

停止确认在独立任务中执行，不能阻塞 watchdog 本身。若 worker 不配合取消或进程关闭未确认，
会话进入阻塞状态并明确提示，避免新旧执行重叠；残留 worker 结束后可用 `/stop` 重试解除。
残留任务保留引用并消费异常，不能将“活动索引释放”报告为“残留协程或进程已经退出”。
24 小时缓存清理仍保留；事件循环本身阻塞时不承诺准点超时。


## 控制会话的 SDK 清理

`/new` 和历史恢复仍调用 `_session(None)`，内部会生成唯一的 `control-` 会话标识，
与普通运行一样登记客户端、初始化任务和所属进程。此标识仅用于资源清理，不新增模型 turn 或用量记录。
退出时先保存 SDK 进程句柄，再执行有超时的正常关闭；失败时调用已有的强制关闭并确认进程退出。
正常关闭失败但强制关闭已确认时，可以继续原业务流程；最终未确认时抛错，不更新后续线程绑定。
调用方取消时，清理独立完成后继续传播取消，重复取消不能使代码越过取消去写库。

初始化尚未结束时不能报告关闭成功。未清理完成的控制会话进入内存维护集合，后台每轮最多处理一个，
失败后等待 30 秒重试；只重试清理，不重放归档或解除归档。该间隔是项目初始策略。
最长保留 24 小时，之后复用过期运行清理流程，尝试关闭并为延迟创建的进程注册终止回调。
未能确认退出时明确记录错误；正常清理成功会释放所有控制会话索引。

数据库绑定不变不等于 SDK 操作回滚：若归档已完成而客户端清理失败，远端线程可能已经归档。
离线回归覆盖关闭失败、强制关闭、绑定更新门禁、重复取消、延迟初始化与后台清理。
