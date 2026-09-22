# middleware 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 目录目标

统一网关消息处理模块的类型、异常、函数边界与日志风格，保持现有消息流转和时序语义。

## 各文件

### `__init__.py`

- docstring 保持简体中文。

### `audio_transcription.py`

- 整理空行与导入；`AUDIO_CONFIG` 等常量补充类型。
- `ASR._run`、`_reap`、`_probe_duration`、`_convert_audio` 补齐返回类型。
- `_extract_text` 根据实际支持的字符串、模型与替身形态补类型；保留兼容性读取，不强制外部返回值变成单一模型。
- 进程创建/取消保留宽泛捕获，但将清理逻辑抽成单一 `_cleanup_process`，并说明必须覆盖 `CancelledError` 的原因。
- 错误消息、docstring 保持简体中文。

### `gateway_commands.py`

- 文件头补 `from __future__ import annotations`。
- 为飞书事件补充 typed `data` 参数，`history_options` 可返回 `list[HistoryOption]`，其中拟议 HistoryOption 为保持既有键的 TypedDict。
- `_stop_chat` 保持 `(states, succeeded, had_work)` 元组及解包顺序，补准确的 tuple 注解；不为风格替换返回形态。
- 只在网络/卡片投递与清理等确定边界捕获异常，其余使用精确异常。
- 提取 `_clear_reaction_safely` 等 helper，减少重复 try/finally。
- 保留现有对外文案和日志字段，说明文字使用简体中文；面向飞书的消息继续使用配置中的中文文案。

### `gateway_execution.py`

- 文件头补 `from __future__ import annotations`。
- 定义运行事件 `TypedDict`，替代零散 `dict` 字符串键。
- `_handle_message_batch`、`_execute_message_batch` 拆分为注册、装配、启动、输出、收尾 helper。
- 外层 `except Exception` 仅作为任务失败兜底，记录类型并保留异常链；正常分支使用 `InputAssemblyError`、`CardDeliveryError` 等精确异常。
- `state`、`batch`、`generation` 类型补齐。
- 保留现有对外文案和日志字段，说明文字使用简体中文。

### `gateway_runtime.py`

- 文件头补 `from __future__ import annotations`。
- 将依赖注入参数改为明确类型；`detached_tasks: set[asyncio.Task]`。
- `_submission_lock`、`_watch_run`、`_cleanup_timeout` 明确返回类型。
- 按调用边界保留异常隔离；后台维护、任务失败和通知兜底不能因收窄捕获而中断后续回收。
- 提取超时和 key 常量，减少魔法字符串。

### `message_assemble.py`

- 可将 `typing.Union` 改为 PEP 604 联合类型；类型别名放在现有模块内，不引入额外依赖。
- 现有 `AssemblyResult`、`_TextPart`、`_ResourcePart` 仅在核查动态属性、弱引用、继承和 mock 兼容后评估 `slots=True`。
- 已有 `_normalize_messages`、`_download_resources`、`_transcribe_audio`、`_build_input_items` 等分段；先补注解并复核边界，仅对仍有重复的部分做局部提取，不再次机械拆层。
- 保留现有对外文案和日志字段，说明文字使用简体中文；飞书提示文案从配置读取。

### `message_collector.py`

- `CollectedMessage`、`MessageBatch` 仅在核查动态属性、弱引用、继承和 mock 兼容后评估 `slots=True`。
- `batch_from_chat_history` 拆出 `_candidate_history_items` 和 `_event_history_item`。
- `_field`、`_is_command`、`is_new_command` 补齐类型。
- 保持边界语义不变；docstring 保持简体中文。

### `message_router.py`

- 文件头补 `from __future__ import annotations`。
- 导入排序；`_bot_identity` 返回 `tuple[str, str]`，`_is_bot_mentioned` 标注入参类型。
- 回调依赖使用 `Callable`/`Protocol` 类型，避免 `Any` 过多。
- `_process_chat_history` 拆出历史获取失败路径，减少嵌套。
- 保留现有对外文案和日志字段，说明文字使用简体中文。

### `resource_validator.py`

- 仅在消除重复注解时定义 `ResourceType`、`HeaderMapping` 类型别名，不改变实际支持范围。
- `ValidatedResource` 保留现有 `frozen=True`；`slots=True` 仅在兼容性核验后评估。
- 格式表常量化并显式类型；`read_resource_bytes`、`validate_downloaded_resource` 补齐返回类型。
- 现有格式、MIME、Office helper 已分离；优先保留结构，仅消除有证据的重复。
- 保留现有对外文案和日志字段，说明文字使用简体中文；保留安全语义，不改变支持格式范围。

## 兼容性与验收

新 TypedDict/Protocol 只描述现有数据，保留 dict 键、tuple 解包、回调及对象引用。提取 helper 不改变锁持有范围、generation 更新、任务注册/移交、消息去重和清理归属。

重点回归：`test_message_assemble.py`、`test_message_collector.py`、`test_resource_validator.py`、`test_audio_timeout.py`、`test_audio_transcription.py`、`test_bot_mention.py`、`test_stop_command.py`、`test_new_command.py`、`test_steer.py`、`test_gateway_startup.py`。覆盖附件顺序/整批上限、Office/MIME 拒绝路径、转写取消、stop 与启动竞态、历史恢复身份以及独立网关实例。
