# 飞书消息卡片

测试前请按根目录 README 安装包，并设置 `FERSK_CONFIG_FILE` 指向 `configs/config_default.json`。

每次飞书 HTTP 请求受 `codex.watchdog.cardRequestTimeoutSeconds` 限制。
推理和进度说明持续展示，最终答案覆盖当前卡正文；任务超时会结束旧流并单独发送失败卡片。
撤回和 `/stop` 在后端确认 thread idle 或运行进程已退出后，发送停止卡片。
未确认停止时发送明确的未确认提示，不报告“已停止”。最终通知按 run 去重；
请求结果不确定时记录日志，不盲目重发，避免用户收到重复卡片。

`lark_card.py` 提供 `await sending_card(union_id, content)`，成功时返回最后一张卡片的消息 ID，空内容返回 `None`。

- `union_id`：来自消息批次的用户 union_id；以 `oc_` 开头时按群 chat_id 发送。
- `content`：字符串直接发送 Card 2.0 通知；异步迭代器中的字符串视为新增文本，`CardReplace(content)` 则替换当前卡正文及缓冲内容。网关内部的 `CardSteer` 控制项暂停写入，等待 steer 结果后决定换卡。
- `session`：可选的 `CardStreamSession`，保存同一个任务的卡片状态、控制通道和停止检查。普通通知与普通文本流无需提供。

```python
from fersk_codex.services.lark.lark_card import sending_card

await sending_card(union_id, "**处理完成**")

async def content_stream():
    yield "第一段"
    yield "\n\n第二段"

await sending_card(union_id, content_stream())
```

样式依据 `project-reference/skills/lark-im/references/card/card-2.0-schema.md` 及同目录的 `lark-im-card-style.md`：使用蓝色标题、标准图标、Markdown 正文、默认卡片宽度，以及参考骨架的正文内边距。

流式实现调用 CardKit 创建卡片、发送 interactive 卡片引用、提交累计正文，最后关闭 `streaming_mode`。首段立即发送，连续片段按 250ms 间隔合并更新。独立计时器在上游静默时也会刷新尾部，不等待下一事件；等待计时器不会取消上游读取。结束时强制刷新尾部，撤回和取消则丢弃缓冲。正文沿用上游 Markdown，不截断，不按长度自动拆分；完整的 Markdown 图片语法 `![说明](https://地址)` 在卡片写入边界降级为裸 `https://地址`，避免飞书将外部地址作为无效 `image_key` 拒绝更新。

每张卡从创建请求开始计时，持续 540 秒后提交缓冲并关闭流式状态；之后有新文本才创建新卡，所有卡片标题统一为“Codex”，没有新文本不发送空卡。续卡仅包含后续文本，不重复旧卡全文；每张卡独立递增请求序号。遇到明确的 `300309` 拒绝时，新建续卡承接未提交后缀（正文替换则承接完整的新正文）。其他交付错误不盲目重试结果不确定的请求：关闭旧卡、停止卡片输出，继续消费模型事件至结束，再抛出 `CardDeliveryError`。网关记录 `delivery_failed`，不会因此中断模型或将已完成的任务改为失败；原有任务期限仍生效。终端记录卡片创建、关闭和错误；逐次更新的序号、字符数及卡龄为 debug 日志。

后端按 `item/started` 保留 agentMessage 的 phase。网关持续展示 reasoning 和 commentary，不因出现进度说明而屏蔽后续推理；工具调用、工具结果和 usage 不展示。最终答案首段覆盖当前卡正文（包括未发送的缓冲），后续答案累积追加；未提供 phase 的旧协议答案兼容原来的替换行为，但仍不会屏蔽之后的 reasoning。已经关闭的旧卡保留历史内容。cmd 仍显示为通知；错误在答案后追加，尚无答案时以错误替换当前正文。输入校验提示和附件通知也使用卡片。撤回时通过 `CardStreamStopped` 丢弃未发送缓冲，保留此前已显示的内容并结束流式状态；已经发出的网络请求无法追回。

应用需要具备消息发送及 CardKit 创建、更新权限。使用现有 `lark_client` 的应用凭据，不新增密钥配置。官方接口说明：[流式更新文本](https://open.feishu.cn/document/cardkit-v1/card-element/content)、[更新卡片配置](https://open.feishu.cn/document/cardkit-v1/card/settings)。

离线行为测试（使用真实 SDK 请求模型、模拟网络响应）：

```sh
python -m unittest discover -s tests -p 'test_lark_card.py' -v
```

## 会话命令

- `/new`：在原始消息入口处理，先停止并确认原任务退出，再归档原 thread 并重置绑定，下一条消息创建新 thread。归档或落库失败会明确提示失败。
- `/stop`：停止当前会话的任务，保留 thread 绑定；下一条消息继续原会话。

两者均使用独立的 `text` 消息，忽略首尾空白和大小写，不接受参数，
不从富文本、语音转写或正文片段中识别。命令由 `messaging.newThreadCommand`
和 `messaging.stopCommand` 配置。群聊继续要求消息事件明确 @ 机器人；
与现有 `/new` 一致，命令文本本身必须独立匹配，不额外移除 mention 占位符。

`/stop` 在网关入口处理，不等待会话锁，也不作为 prompt 提交 Codex。
它与消息撤回复用 interrupt 和卡片停止流程：关闭输出、丢弃未发送缓冲、
清理 reaction。停止前接收的待处理请求及附件缓冲失效，停止后新输入正常执行；
历史收集遇到 `/stop` 或 `/new` 时停止向前收集。撤回仍只停止对应消息所属批次，
且仅处理 `message_owner` 撤回事件。

反馈区分已请求停止、当前空闲和中断请求失败；失败时仍拦截后续输出。
停止不能撤销已经发送的网络请求或已完成的工具操作。群聊按 chat_id 共享任务，
因此有效的 `/stop` 停止该群当前任务。

中断和命令回归测试：`python -B -m unittest discover -s tests -p 'test_stop_command.py' -v`。

## 消息处理 reaction

每条接收消息添加处理中的 emoji，成功返回的 reaction ID 按会话和消息 ID 分别保存。
批次结束（包括输入处理失败）时逐条清理该批次的 reaction，保留其他批次和会话的记录。
撤回先清理对应消息，批次退出时清理其余消息；并发清理不会重复提交同一个删除请求。
`/stop` 清理停止时已记录的消息快照，停止后新消息不受影响；停止前发起、停止后才返回的
添加请求会单独清理。

删除接口返回成功后才移除本地记录；单条失败不影响其余消息，失败记录保留在内存中，
供后续针对该消息的清理或 `/stop` 再次尝试。没有后台自动重试或跨进程持久化。
多消息批次、停止、撤回及清理失败的回归测试包含在上述 `test_stop_command.py` 中。

## 任务进行中补充消息

发送任务后再次输入，会在输入组装完成时查询原 thread 的实际状态。
运行中（`active`）将新增输入作为 steer 加入原 turn，复用原回复流；
没有执行中的任务或任务已结束时正常开始下一 turn。首次启动尚未完成时，
后续输入等待启动窗口结束再判断。steer 不会中断当前任务并重启。

steer 明确成功后立即关闭旧卡、丢弃未发送的旧正文缓冲，发送新的流式卡片，
即使工具执行期间没有新文本也会换卡。旧卡已经显示的内容保留，不撤回消息。
新卡先显示 `messages.steerAccepted`，首段后续正文替换占位，最终答案仍覆盖当前卡。
每次换卡重置正文分隔与答案状态，新卡独立计算 9 分钟期限；任务总期限不续期。
如果成功后没有后续正文便结束，新卡以 `messages.steerCompleted` 结束，避免残留处理中提示。

卡片写入、换卡及计时关闭由唯一发送协程串行处理。steer 请求期间暂停显示写入，
保留最多一个预取事件；返回失败或 idle 时恢复旧卡，不丢弃其缓冲。
成功后换卡交付失败沿用 `delivery_failed` 策略，不重新提交 steer。
停止、撤回会释放待处理控制并拦截后续工作卡片；已经发出的网络请求无法追回。

图片/文件继续按原有窗口合并，语音先转写再提交。steer 不支持切换模型，
当前模型与配置的图片模型不同时会提示重新发送，而不是静默启动第二个任务。
`/new` 主动停止原任务并确认退出后执行归档与重置；`/stop` 可随时停止，保留 thread 绑定。
撤回已接受的补充消息会停止它所属的整个任务，无法单独撤销已执行的补充指令。

历史批次以触发消息为上界，避免提前提交更晚的消息。网关按消息 ID 对接收事件和
已处理批次去重，防止同一附件或文本被历史重叠再次提交；每个会话的两个记录各保留
最近 `messaging.recallCacheMaxEntries` 条，仅在当前进程有效。
新卡改善聊天顺序，但防重仍按消息 ID 执行；即使历史接口暂未返回新卡，
已经接受的 steer 消息也会从下一批输入中排除。该去重没有跨进程持久化保证。
处理失败的旧消息不会随下一批历史自动重试，需要用户重新发送。
steer 消息的处理中 reaction 随原任务完成、停止或撤回统一清理。

## 输入路由与音频转换期限

`/new` 与 `/stop` 均在原始 text 事件入口识别，不进入历史组装和模型 prompt。
富文本或语音转写结果即使只有 `/new`，也按普通输入处理。重置期间的新输入等待重置
结束后再提交；命令前的缓冲及待处理输入失效。

路由只按 `messaging.directTypes` 与 `bufferedTypes` 执行。不支持的消息单独提示并清理
reaction，不取消附件缓冲，也不混入后续历史批次。群成员继续共享历史，历史窗口仍为 10 条。

`audio.limits.conversionTimeoutSeconds` 默认 540 秒，按单个音频从 ffprobe 检测至全部
ffmpeg 转码/切片完成累计计时，切片或重试不续期；不包含 ASR 网络转写时间。
超时、撤回、停止或上层取消时，异步子进程先终止，2 秒未退出则强制结束并回收，再删除
临时目录。超时通过附件通知卡片明确提示转换失败，不作为转写文本交给模型。
全部附件不可用时，连同任务文本一起不提交；部分可用时保留其余输入。
现有任务总期限仍有效，可能先于转换期限触发。
