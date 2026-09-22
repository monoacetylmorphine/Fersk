# services 与 services/lark 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 目录目标

按参考项目风格重写飞书客户端、请求执行、交互卡片、消息卡片和消息工具，保持 Lark API 调用行为不变。

## 各文件

### `services/__init__.py` 与 `services/lark/__init__.py`

- docstring 保持简体中文。

### `lark_client.py`

- 文件头补 `from __future__ import annotations`，整理导入。
- `_required_setting`、`create_websocket_client` 补齐类型。
- 可以提取私有构造 helper，但继续在现有时机创建模块级 `client`；改成延迟工厂会改变凭据缺失报错时机和共享实例生命周期，另行评估。
- 保留现有对外文案和日志字段，说明文字使用简体中文。

### `lark_interactive_card.py`

- 文件头补 `from __future__ import annotations`，导入排序。
- `HistoryCard` 补字段类型并有条件评估 slots；`HistoryCardStore` 是普通类，保留构造器和可变 cards 字典，不为 slots 转成 dataclass。
- `visible_options` 明确 `tuple[dict[str, object], ...]`，避免裸 `tuple[dict, ...]`。
- `resolve`、`confirmed_thread_id`、`build_history_card` 明确返回类型。
- 保留 `message_event()` 的 SimpleNamespace 适配，它提供恢复入口需要的嵌套事件字段及服务端身份上下文；类型可用 Protocol 描述，不冒充真实 SDK 事件。异常按现有调用契约处理。
- 简体中文 docstring 与日志；飞书卡片文案保持配置中的中文。

### `lark_message_card.py`

- 文件头补 `from __future__ import annotations`。
- `CardRequestError`、`CardStreamStopped` 保持为独立异常类，补充类型化字段。
- `CardReplace` 保留 frozen dataclass；`CardStreamSession` 是管理队列与生命周期的普通类，补字段注解，暂不改变对象布局。补充 `CardSteer` 的类型与确认/释放语义核验。
- `_card` 返回 `dict[str, object]`，把当前的大卡片结构拆成 `_card_config`、`_card_body` 等 helper。
- `_call`、`_send`、`sending_card` 明确返回类型；仅在网络响应不确定时捕获宽泛异常。
- 删除被注释掉的旧卡片结构和冗余说明；保留 CardKit 时序说明。

### `lark_requests.py`

- 文件头补 `from __future__ import annotations`。
- `executor` 类型标注；`call_lark` 明确返回类型。
- 双包条件导入保持，但注释说明原因，且保留现有对外文案和日志字段，说明文字使用简体中文。

### `lark_tools.py`

- 文件头补 `from __future__ import annotations`，删除 `from lark_oapi.api.im.v1 import *`，改为显式导入。
- 修正参数类型写法，例如 `message_id: str`。
- 删除 `# 发起请求`、`# 处理失败返回`、`# 处理业务结果` 等重复注释。
- `_save_resource` 已存在，仅补类型；如失败处理确有相同语义，再提取 `_log_lark_failure(response, action)`。
- `adding_reaction_emoji`、`delete_reaction_emoji`、`download_msg_resource`、`getting_chat_history` 明确返回类型。
- 网络失败保持当前抛出/返回契约，不把返回失败改为抛异常；保留现有对外文案和日志字段，说明文字使用简体中文。

## 兼容性与验收

本文件覆盖 services 下两级包入口及 lark 子目录。保留飞书请求字段、收件目标、CardKit sequence/限频/收尾时序、失败回退和返回值；保留 HistoryCard 的服务端身份校验，不能信任客户端回传的身份字段。

重点回归：`test_lark_interactive_card.py`、`test_lark_message_card.py`、`test_lark_tools.py`、`test_reaction_api.py`、`test_request_limits.py`、`test_business_logger.py`。核查 SDK 显式导入清单、凭据缺失报错、日志脱敏、流式卡片与 steer 确认/释放、reaction 失败后重试和请求超时后的线程容量。
