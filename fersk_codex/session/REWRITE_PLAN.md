# session 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 目录目标

统一会话历史、线程绑定和任务缓存代码的类型、错误处理与命名，保持数据库和行为不变。

## 各文件

### `__init__.py`

- docstring 保持简体中文。

### `session_codex.py`

- 文件头补 `from __future__ import annotations`。
- `thread` 参数注解使用 SDK 公开的 `AsyncThread`，不是同步 `Thread`；`prompt` 使用明确类型别名。
- `_initialize_session_name`、`_sync_session_time`、`restore_session`、`reset_thread` 补齐返回类型。
- 保留 `_sync_session_time` 的 best-effort 捕获；初始化命名和恢复/重置失败继续按原路径传播。不得把两类失败统一吞掉或统一抛出。
- docstring、保留现有对外文案和日志字段，说明文字使用简体中文。

### `session_gateway.py`

- 文件头补 `from __future__ import annotations`。
- `ReactionCleanup`、`ActiveCodexRun` 保留可变 dataclass，`slots=True` 延后评估。
- 当前 `ActiveCodexRun` 未发现重复字段，且必填字段位于默认字段之前；删除无依据的修复任务。仅补齐 `cards`、`probe` 等注解，使用 TYPE_CHECKING 防止运行时循环导入。
- `SessionCache` 保留现有属性名、共享引用和 dict/set，逐项补注解，不迁移为新的状态对象；`hold`、`remember`、`release_idle`、`prune` 补齐类型。
- 仅在缓存清理/后台任务中保留宽泛异常，并在注释中说明原因。
- 简体中文 docstring 与日志。

### `session_history.py`

- 文件头补 `from __future__ import annotations`。
- `SessionRecord` 保留现有 `frozen=True`；`slots=True` 仅在兼容性核验后评估。
- `_connect` 返回 `AsyncIterator[aiosqlite.Connection]`；SQL 常量集中。
- `make_thread_name` 拆出字素簇截断逻辑；`register_session`、`get_session`、`list_sessions` 等补齐类型。
- 不为自然传播的 SQLite/ValueError 新增无意义 catch；日志/docstring 保持简体中文。

## 兼容性与验收

不得改变数据库字段/排序规则/事务、类级运行状态归属、缓存 TTL、锁的共享范围、清理顺序与 reaction 延后删除。普通注解使用 TYPE_CHECKING；不得在 session_gateway 中为了类型引入运行时 services/codex 依赖。

重点回归：`test_session_cache.py`、`test_session_history.py`、`test_thread_management.py`、`test_sdk_session.py`、`test_new_command.py`、`test_stop_command.py`、`test_steer.py`。保留字素簇截断、同秒排序、命名竞争、取消传播、待处理请求保护、24 小时过期及失败控制会话清理。
