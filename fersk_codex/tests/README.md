# 测试

在已安装项目 `pyproject.toml`／`uv.lock` 依赖的 Python 3.13+ 环境中，从项目根目录运行：

```sh
python -B tests/run_tests.py
python -B tests/run_tests.py --pattern test_message_assemble.py -v
python -B tests/run_tests.py --reverse
```

测试入口使用 `configs/config_default.json` 生成临时配置，将数据库、工作区和运行日志定向到临时目录，不加载运行环境的 `.env`。不需要启动飞书、Codex 或语音识别服务。原有进程清理测试会启动短暂的本地 Python 子进程。

`--reverse` 反向执行测试，用于检查模块缓存、全局状态和 mock 是否影响其他用例。未匹配任何测试或测试失败时返回非零退出码。仍可使用原来的 `unittest discover -s tests`，但该方式需要自行设置包导入路径和 `FERSK_CONFIG_FILE`。

新增覆盖包括：资源签名/MIME/文件名校验，历史排序与消息边界，富文本和多模态组装，下载与转写失败、取消清理，音频分段边界，配置错误，飞书 SDK 请求参数，SQLite 持久化以及重试退避。

用量测试覆盖逐事件提交 `last` 增量、`runId` 分组、旧表追加字段且保留历史行、结束后耗时回填、异常退出与重复取消不重复插入，以及无用量事件时不写零值行。字段口径见 [用量记录说明](../docs/TOKEN_USAGE.md)。

本次回归覆盖共享配置的完整校验与符号链接来源、并发首次启动不覆盖配置、`maxRunSeconds` 正整数约束、慢 Git 初始化期间另一会话正常完成、Git 超时及取消回收、附件写入不阻塞事件循环，以及任务结束不生成 CSV。模型流程测试模拟 `prepare_workspace`，真实子进程由工作区专用测试验证。

缓存生命周期回归覆盖：结束后的状态释放与 ID 去重、等待请求保留锁和 generation、steer 所属任务保护、
无新消息时的缓冲过期、停止未确认且关闭失败时强制清理、延迟初始化进程清理、历史和卡片请求失败、
日志写失败保留与 24 小时到期丢弃，以及共享分页参数、富文本双 content 去重和整批附件上限。
24 小时场景使用可控时钟，无需实际等待；不做容器启动测试。

可靠性回归增加：终态后流挂起、不配合取消的卡片协程、停止确认挂起、线程超时后仍占用名额、
5 个同步请求并发、入站控制事件预留容量、业务日志等级和 SDK DEBUG 凭据脱敏。
`test_request_limits.py` 同时验证 Codex 与 MCP 的执行器；MCP 自身的 `test_runtime.py` 另验证
上传超时后的文件句柄生命周期。全部使用模拟请求，不调用真实飞书服务或模型。


控制会话、reaction 和附件校验修复的回归分别位于 `test_sdk_session.py`、
`test_session_cache.py`、`test_resource_validator.py`，并复用原有停止、steer、附件组装与下载测试。
控制会话测试验证关闭未确认时不写绑定、取消不被吞掉及后台只重试清理；reaction 测试验证
卡片结束前不删除、交付失败后也删除、失败记录跨任务释放保留及无新输入时后台重试。
Office 样例在内存中构造真实 ZIP 包及必要元数据，覆盖伪 ZIP、缺失部件、类型不匹配、
元数据大小和实体声明；文本覆盖保留扩展名、二进制伪装和 JSON/JSONL 区分。

Codex 拆分后的回归直接使用 `codex_execution.FerskCodex`；SDK 客户端在
`codex_runtime` 中 mock，会话元数据同步在 `session_codex` 中 mock，执行与用量依赖在
`codex_execution` 中 mock，线程绑定在 `thread_manager` 中 mock。

网关拆分测试通过 `main.create_gateway()` 创建独立实例，mock 指向 runtime、execution、commands
或 router 的实际依赖，不再替换旧 `gateway` 模块的全局变量。共享缓存、停止与撤回、steer、
卡片交付、历史恢复和过期清理沿用原回归场景；启动测试验证组件隔离、事件注册和后台任务退出。
