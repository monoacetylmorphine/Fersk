# tests 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 总体策略

- 保留 unittest / IsolatedAsyncioTestCase；当前 `run_tests.py` 使用 unittest discovery，不能发现 pytest 函数，当前 `.venv` 也未安装 pytest，项目未声明 pytest 依赖。
- 本阶段只改善测试命名、类型、重复 setup/helper 和场景组织；保留文件名、TestCase 结构、测试方法及关键断言，使用现有 subTest 做数据驱动覆盖。
- 保留 IsolatedAsyncioTestCase 的事件循环隔离、async cleanup 和 pending-task 检查；不随意替换成 asyncio.run 或引入异步插件。
- 保留临时配置、临时数据库、禁用 dotenv 和包加载机制；SDK/Lark/OpenAI 请求与安装动作使用离线 mock。已有本地短命子进程、SQLite 和文件系统测试保持真实，不误写成“所有 I/O 均 mock”。
- 测试随相关生产模块修改逐批执行，先比较原有场景与测试数量，最后进行正序、反序全量回归；不只以数量判断覆盖率。
- pytest 迁移单列后续提案：需要同时批准依赖声明、测试入口、执行说明及必要 fixture 文件的范围，保证收集覆盖与临时配置在模块导入前生效。未批准前不执行。
- 以下用例为未来实施的验收范围，不代表本轮已执行。历史测试结果不能替代新基线。

## 各文件

| 文件 | 改写要点 |
| --- | --- |
| `run_tests.py` | 保留离线 unittest 入口及命令行选项，描述与实际执行器一致；`reverse_suite`、`main` 补齐类型。 |
| `test_audio_timeout.py` | 保留现有 setup/helper 与 assertRaises，验证超时、取消和进程回收。 |
| `test_audio_transcription.py` | 保留 IsolatedAsyncioTestCase 的循环与清理隔离；保留 ffmpeg/网络 mock。 |
| `test_bot_mention.py` | 用 subTest 覆盖 mention 组合，mock 身份环境变量。 |
| `test_business_logger.py` | 保留 SDK 日志等级独立与脱敏断言，保留 TestCase 和关键断言。 |
| `test_codex_retry.py` | 重试测试可用 subTest 合并同构场景，保留异常、取消和退避断言。 |
| `test_config.py` | 保留 subTest，避免引入测试收集变化；保留配置安全和缺失字段覆盖。 |
| `test_daily_logs.py` | 使用现有 mock/setup 控制时钟和单写者，保留跨 UTC+8 边界场景。 |
| `test_gateway_startup.py` | 保留独立网关与 IsolatedAsyncioTestCase，整理重复 setup。 |
| `test_lark_interactive_card.py` | 用 subTest 验证 resolve/confirm/翻页；保持真实 SDK 模型 mock。 |
| `test_lark_message_card.py` | 拆成卡片构建、流式发送、steer 竞态三类 TestCase 测试方法。 |
| `test_lark_tools.py` | 保留 TestCase；为每个 Lark 工具提供独立 setup/mock。 |
| `test_message_assemble.py` | 保留真实装配管线，参数化附件与拒绝组合。 |
| `test_message_collector.py` | 历史排序、边界命令测试参数化。 |
| `test_model_routing.py` | 使用配置 patch 和 subTest，验证模型选择不触发网络。 |
| `test_new_command.py` | 保留异步 TestCase，覆盖 `/new` 停止、等待和隔离。 |
| `test_office_workspace.py` | 保留离线工作区初始化测试，mock 子进程与锁文件。 |
| `test_package_paths.py` | 保留现有包路径回归及 unittest 断言。 |
| `test_reaction_api.py` | 参数化 reaction 创建/删除成功、失败与缺失字段。 |
| `test_request_limits.py` | 使用 AsyncMock 和现有 setup/helper 验证真实线程容量与事件准入。 |
| `test_resource_validator.py` | 使用 subTest 覆盖格式签名、MIME、Office 元数据和文本校验。 |
| `test_sdk_session.py` | 保留 unittest，验证 SDK 会话配置与控制会话清理。 |
| `test_session_cache.py` | 生命周期边界测试参数化，避免真实等待 24 小时。 |
| `test_session_history.py` | 临时 SQLite setup/cleanup；名称、排序、命名竞态测试参数化。 |
| `test_shared_config.py` | 保持共享配置初始化、双包导入和保护测试。 |
| `test_steer.py` | 大型 steer 测试整理 backend/gateway/collector helper，保留循环隔离，保留竞态与订阅断言。 |
| `test_stop_command.py` | `/stop`、撤回和启动竞态拆成独立 TestCase 方法。 |
| `test_thread_management.py` | 临时 SQLite setup/cleanup，验证绑定持久化和重置。 |
| `test_turn_usage.py` | 参数化增量用量和异常退出场景，保留 SDK 数据模型。 |
| `test_usage_logging.py` | 临时 SQLite setup/cleanup；保留表迁移、字段填充和写入失败的关键断言。 |
| `test_watchdog.py` | probe/journal 测试拆为纯函数、异步确认、进程关闭三类。 |
| `test_workspace.py` | 使用临时目录及现有 setup 构造 workspace，mock git/uv/pnpm 子进程。 |

## 验收方式

从项目根目录使用 `.venv/bin/python -B tests/run_tests.py` 和同命令加 `--reverse`；单文件使用 `--pattern test_*.py` 的实际文件名。先检查失败清单、收集数量、跳过项及退出码，再与基线比较。保留 `--pattern` 未匹配时报错行为。共享配置及请求容量用例涉及兄弟包；缺少依赖时报告环境阻塞，不能跳过后宣称全量通过。
