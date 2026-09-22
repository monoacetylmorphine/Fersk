# codex 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 目录目标

将 Codex SDK 的会话生命周期、线程绑定、工作区准备、运行探针和重试逻辑改写为参考项目风格：更强的类型、职责明确的函数、保持契约的异常处理、说明约束的简体中文注释。

## 各文件

### `__init__.py`

- docstring 保持简体中文，说明该包管理 Codex SDK 集成。
- 保持该包按需导入，不新增 eager re-export；顶层包已有 `FerskCodex`、`LiveTurn` 延迟导出，保持路径与触发时机。

### `codex_execution.py`

- 文件头补 `from __future__ import annotations`，整理标准库、SDK、本地模块导入顺序。
- `prompt` 注解可复用 SDK 公开的 `InputItem`，拟定义 `CodexPrompt = str | list[InputItem]`；不存在已核验的 `CodexInputItem` 公共类型。不得把接受范围扩大为 SDK 的所有输入形态。跨模块复用不得新增循环导入。
- 提取模型选择逻辑为私有函数，优先保持简单返回值；仅在确有收益时新增内部 `ModelSelection`。保留纯文本列表、空列表、MentionInput 和含图片输入的当前路由优先级。
- `_retry_on_overload_async` 参考 `openai_codex.retry.retry_on_overload`：类型变量和配置默认值已存在，核对其注解、异常保真及取消语义；不改配置退避策略或日志文案。
- `_error_event` 用 `TypedDict` 注解，运行时仍返回 dict，保留 `type/code/content` 字段；不能换成 dataclass 破坏下游索引。
- `_save_turn_usage` 保持有限超时、重复取消隔离、失败不重试已可能提交的写入，以及最终重新传播取消；保留持久化失败不覆盖主流程的边界捕获。
- `FerskCodex.running` 拆成 `_bind_thread`、`_prepare_workspace`、`_start_thread`、`_stream_turn` 等私有方法，减少单个函数长度和重复 `except`。
- 去掉重复的 `if isinstance(prompt, str)` 分支写法，统一由 `_resolve_model` 处理。
- 保留现有日志文案，docstring 使用简体中文；面向飞书的错误内容继续从配置读取。

### `codex_runtime.py`

- 文件头补 `from __future__ import annotations`，整理导入。
- `LiveTurn` 保留可变 dataclass，补充字段注解；`slots=True` 延后评估，`thread` 与 `handle` 分别使用公开 `AsyncThread`、`AsyncTurnHandle`（替身可用最小 Protocol）；补充 `-> None`。
- 保持类级共享状态、继承关系与对象身份，仅补注解，例如：`dict[str, LiveTurn]`、`dict[str, asyncio.Task]`、`dict[str, tuple[float, float]]`。
- `_session` 明确 `AsyncIterator[AsyncCodex]` 返回类型，拆分关闭确认逻辑为 `_close_session`。
- `completed_status` 当前返回经 `.value` 归一化的状态字符串或 None，优先注解为 `str | None`，有充分约束后再用 Literal；不能直接声明为 `TurnStatus | None`。对现有私有 SDK 进程访问集中说明版本依赖，不扩大私有 API 使用。
- 逐一记录 `cleanup_control_sessions`、`force_close`、`interrupt_and_confirm`、`steer` 的异常与返回契约；保留停止未确认、清理失败和通知隔离的兜底，不机械收窄异常。
- 使用 `asyncio.timeout`、`asyncio.shield` 时说明为什么需要屏蔽取消，避免无谓注释。

### `codex_workspace.py`

- 文件头补 `from __future__ import annotations`，常量和默认值补充类型。
- `PYTHON_PACKAGES`、`NODE_PACKAGE`、`_MANIFESTS`、`_PYTHON_MODULES` 使用 `tuple`、`dict[str, object]` 等准确类型。
- `_reap` 的进程来自 `asyncio.create_subprocess_exec`，应注解为 `asyncio.subprocess.Process`，不能用 `subprocess.Popen[str]`；`_run` 根据实际输出解码与 CompletedProcess 构造方式确定返回泛型。
- 将安装前冲突检查、状态文件读写、锁文件校验拆为独立私有函数，避免超长函数。
- 保留现有对外文案和日志字段，说明文字使用简体中文；`prepare_workspace` 保持签名与行为不变。

### `thread_manager.py`

- 文件头补 `from __future__ import annotations`。
- `_connect` 标注 `AsyncIterator[aiosqlite.Connection]`。
- SQL 常量集中定义；`get_user_thread`、`set_user_thread` 明确返回类型。
- 数据库错误当前自然向外传播，不存在此处吞错的证据；无转换需求时不新增 catch。保持 WAL、连接超时、提交边界和 None 重置语义。
- docstring 保持简体中文。

### `thread_watchdog.py`

- 文件头补 `from __future__ import annotations`。
- `settings()` 保留字典访问并补充相应 TypedDict 或字典注解；`should_log_event`、`summarize_item`、`summarize_event` 补齐注解。
- `RunProbe` 保留可变 dataclass，补充字段注解；`slots=True` 延后评估，字段使用 `set[str]`、`int` 等准确类型。
- `RunJournal` 队列、锁和线程字段类型化；`_write_pending` 拆分写入、截断和丢弃逻辑。
- 保留后台写入、flush 与任务收尾已有隔离边界；缩窄捕获前先证明错误传播与缓冲保留行为不变。
- 保留现有日志文案，docstring 使用简体中文。

## 兼容性与验收

提取 helper 时逐项保留 await/yield 顺序、线程绑定先于 turn 提交、恢复与解归档回退、stream 已启动后不重提 turn、用量逐事件增量及终态回填。SDK retry.py 本身也捕获 Exception 后分类，不要求删除同类边界捕获；异步重试不得调用同步 sleep。

重点回归：`test_codex_retry.py`、`test_model_routing.py`、`test_sdk_session.py`、`test_watchdog.py`、`test_workspace.py`、`test_office_workspace.py`、`test_turn_usage.py`、`test_steer.py`、`test_stop_command.py`。控制会话关闭未确认不能写绑定；重试、取消、进程树回收、清理超时与 24 小时过期行为均须保留。不得运行真实用户工作区安装来验证风格。
