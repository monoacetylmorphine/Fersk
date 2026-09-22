# utils 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 目录目标

统一线程池、事件分发、日志输出和用量持久化的类型、命名与错误处理。

## 各文件

### `__init__.py`

- docstring 保持简体中文。

### `bounded_executor.py`

- 文件头补 `from __future__ import annotations`。
- `RequestCapacityError` 明确继承关系与 docstring。
- `BoundedExecutor.__init__(capacity: int = 8)` 显式类型。
- `call` 的 `operation`、`args`、`timeout` 与返回值补齐类型；可用 `Callable[..., T]` 与返回 `T` 保留结果类型，`timeout` 根据实际允许值注解。
- 保留 `BaseException` 以覆盖取消和提交失败；必须区分提交失败立即释放与真实线程完成回调释放，不能合并到协程 finally 造成提前或重复释放。
- 简体中文 docstring。

### `event_dispatcher.py`

- 文件头补 `from __future__ import annotations`。
- `EventDispatcher.__init__` 使用 `AbstractEventLoop`，容量参数显式 `int`。
- `submit` 的 `handler`、`data` 类型化；`done` 回调类型化。
- 保留取消安全逻辑；日志/docstring 保持简体中文。

### `logger.py`

- 文件头补 `from __future__ import annotations`。
- `RedactingFormatter.format(record: logging.LogRecord) -> str` 显式返回类型。
- `protect_sdk_logs`、`configure_logging`、`get_logger` 补齐类型。
- 只有结构确实更清晰时提取 `_configure_root_logger()`，保持原配置时机、重复调用行为及 handler 数量，不能重复注册。
- docstring 保持简体中文。

### `logging.py`

- 文件头补 `from __future__ import annotations`，整理导入。
- 删除 `typing.Dict`，使用 `dict[str, Any]`。
- `DEFAULT_VALUES`、`REQUIRED_KEYS` 常量类型化。
- `ensure_keys`、`SavingLog`、`finalize_usage` 补齐类型；`SavingLog` 保留兼容名称但改为简体中文 docstring。
- `SavingLog` 已捕获 `aiosqlite.Error` 后重新抛出；保持这一语义、原事务及 finalize_usage 的 UPDATE 行为，不新增迁移或自动重试写入。

## 兼容性与验收

保留线程上下文传播、真实线程完成前占用容量、控制事件预留容量和回调线程边界。日志层保持 SDK 脱敏、业务等级独立及 handler 注册时机；不得为减少 catch 让日志失败覆盖模型结果。参数来源注释必须保留。

重点回归：`test_request_limits.py`、`test_business_logger.py`、`test_usage_logging.py`、`test_turn_usage.py`、`test_daily_logs.py`。确认取消/超时不超额准入，单次提交失败仅释放一次，旧表数据保留、重复取消不重复写入，以及跨日行为不变。
