# fersk_codex 代码重写规划

状态：2026-09-22 完成规划审查；本轮仅修改 8 份 REWRITE_PLAN.md，未实施代码重写。

## 审查结论

方向可行，但原稿不能直接执行。类型补全、显式导入和局部 helper 提取适合分批实施；pytest 迁移、配置模型替换、统一序列化、异常策略收窄及状态容器迁移混入了行为或工具链变更，已移出默认实施范围。后续代码修改须另行授权，本规划不构成执行授权。

## 参考依据与核验边界

- 用户已确认 `coding-reference/src/openai_codex` 指官方 Codex Python SDK package。该本地路径在当前项目及已检查的相邻位置未找到，因此改从 `.venv` 中的同一官方包读取源码；未核实原参考副本的版本、Git commit、Ruff 配置或测试框架。
- 本轮以当前 `.venv` 安装的 `openai-codex==0.155.1` 源码作为可复现的风格样本，Python 为 3.13.15。发行包 METADATA 标明 OpenAI 作者及 `https://github.com/openai/codex` 仓库；这不是对远端最新 commit 的核验。
- 已检查 `openai_codex/__init__.py`、`api.py`、`_inputs.py`、`client.py`、`async_client.py`、`retry.py`：存在显式公共导出、现代注解、用途不同的 dataclass、协议边界序列化及按可重试性分类的异常捕获。生成代码不是业务代码的排版模板。
- [官方 SDK 文档](https://learn.chatgpt.com/docs/codex-sdk)确认 Python SDK 的异步入口；[官方开源说明](https://learn.chatgpt.com/docs/open-source)确认仓库归属。网页不用于推断未核实的 Python lint 规则。
- 后续如取得原参考 checkout，先记录版本或 commit，再核对风格差异；不随本次风格重构升级 SDK。

## 目标与边界

- 参考上述已核验的 OpenAI Codex Python SDK 风格，进行保持行为的渐进式整理。
- 当前授权仅限 REWRITE_PLAN.md。未来获准实施后，只在批准的现有 Python 文件中增量修改，不新增、删除或移动 Python 文件；不修改依赖清单、JSON 配置、数据库格式或目录结构。
- 保持现有模块入口、对外调用方式和运行行为不变；本次属于非功能性风格重构。
- 本目录新增的 `REWRITE_PLAN.md` 及子目录同名文件仅为规划文档，不参与运行。

## 统一采用的参考风格

- 所有模块优先使用 `from __future__ import annotations`。
- 类型使用 PEP 604 联合写法 `str | None`，不再使用 `Optional`、`Dict` 等旧式 `typing` 写法。
- 保留现有对象形态。字典优先补充 TypedDict，外部可替换依赖可用 Protocol；不为风格引入 Pydantic 配置模型。`slots=True` 须先核查动态属性、`vars()`、弱引用、继承和 mock；`frozen=True` 只适用于确实不可变的值，不用于运行状态。
- SDK 的 `client._params_dict` 在协议请求边界使用 `model_dump(by_alias=True, exclude_none=True, mode="json")`；本项目逐调用点保持现有参数，不能统一套用。用量持久化依赖 snake_case 字段，展示数据也须保留当前键名和空值语义。
- 公共函数、私有函数、类方法补齐返回类型；`None` 返回值显式写 `-> None`。
- 新的内部变量与 helper 使用 `snake_case`；保留公共符号、关键字参数名、现有 `SavingLog` 名称及配置、SQL、事件的既有键名。
- 导入按标准库、第三方、本地模块分组，但保持有副作用的导入顺序及双包条件导入。行长 100 可作为本项目建议目标；`E, F, I, B, C4` 和 isort 合并规则的官方来源未核实，不设为验收门槛，也不新增工具依赖或批量自动修复。
- 按边界决定异常捕获范围。重试分类、事件生成器、日志持久化隔离、后台任务和资源回收允许保留 `Exception`；取消后仍须清理的路径可保留 `BaseException` 并重新抛出。不能以未知异常外泄替代既有错误事件。只有已经需要转换异常的边界使用 `raise ... from exc`，否则保留原异常类型。
- 规划、说明和 docstring 使用简体中文；标识符与技术术语使用英文。保留对外错误、飞书文案、日志事件标识与关键字段，不做批量翻译；英文不是采用 SDK 结构风格的前提。
- 可删除机械重复注释，但保留参数来源、用户约束、取消和时序说明。未知参数来源标注 `provenance: unknown`，不能把来源标记当作冗余信息删除。
- 第一阶段只给现有全局和类级状态补注解，不改变共享范围、对象身份或锁。集中迁移为实例/dataclass 属于另行评估的生命周期变更。使用 TYPE_CHECKING 和前向引用避免类型注解引入循环导入或启动副作用。

## 本目录文件

| 文件 | 改写要点 |
| --- | --- |
| `__init__.py` | 保留简体中文 docstring；显式说明包用途；`__getattr__` 补充返回类型和 docstring；延迟导入保持不变。 |
| `main.py` | `main()`、`cli()` 和多数事件回调已有注解，仅补遗漏项；必要时提取局部 helper，保持事件注册顺序、后台任务退出和独立网关装配；不机械收窄退出兜底。 |

## 子目录计划

- [codex](codex/REWRITE_PLAN.md)
- [configs](configs/REWRITE_PLAN.md)
- [middleware](middleware/REWRITE_PLAN.md)
- [services](services/REWRITE_PLAN.md)
- [session](session/REWRITE_PLAN.md)
- [utils](utils/REWRITE_PLAN.md)
- [tests](tests/REWRITE_PLAN.md)

## 实施顺序与验收门槛

1. 先建立基线：记录 Python/SDK 版本、测试数量、失败用例和耗时。保留 unittest 及其临时配置入口，测试随每批生产代码修改，不放在最后整体迁移。
2. 先做低风险注解和显式导入，每批一个模块及必要测试；共享配置模块须同时核对 `fersk_mcp` 消费路径，不以“先稳定配置”为由替换配置模型。
3. 再整理 `codex`、`session`、`services/lark` 的局部函数边界；最后整理依赖这些模块的 middleware 和 `main.py`。Codex 与 session 有相互依赖，按调用链验收，不强行设定整目录迁移顺序。
4. 每批执行相关测试，阶段结束执行正序和反序全量回归；验收通过再继续下一批。失败时停止扩大范围，区分基线失败和新失败。
5. 需要改公共接口、配置/数据库格式、工具链或跨模块行为的提案单列，先说明文件范围与影响并取得批准。

未来实施时的回归命令（本轮未运行，不代表通过）：

```sh
.venv/bin/python -B tests/run_tests.py
.venv/bin/python -B tests/run_tests.py --reverse
.venv/bin/python -B tests/run_tests.py --pattern test_codex_retry.py -v
git diff --check
```

- 单批验收保留调用签名、返回形态、导出和 mock 路径；静态注解不能无意触发客户端初始化。
- 异步验收覆盖重复取消、启动与 stop/steer 竞态、终态后流挂起、超时后线程仍占容量、清理失败后的后续回收；保持 await/yield、锁持有范围和资源所有权。
- 数据验收覆盖配置路径、符号链接共享、SQL 事务/排序/空值、usage 增量与只回填不重复插入；不以修改断言迁就行为变化。
- 测试数量不得无解释减少；同时核对原场景和关键断言。`tests/README.md` 记载的历史 SQLite WAL 竞争只是待复现线索，不能当成本轮结果或默认豁免。
- 本轮完成文档与代码静态对照、安装版 SDK 源码核验；未执行应用、模型/Lark 请求、生产回归或部署验证。代码可运行性仍须在实施阶段验证。
