# configs 目录重写规划

状态：2026-09-22 已审查；当前只允许修改规划，未实施代码修改。

执行边界、参考版本及例外以[总计划](../REWRITE_PLAN.md)为准。下列 helper/类型名称为拟议名称，不代表现有实现；已有注解或拆分只核对，不重复改写。

## 目录目标

按参考项目风格重写配置加载、Schema 校验和共享配置初始化。保持 `CONFIG` 为原有可变 dict、嵌套键与加载时机不变；本阶段只补注解，不迁移配置对象。

## 各文件

### `__init__.py`

- docstring 保持简体中文，说明包职责；不读取配置、不创建运行资源。

### `loader.py`

- 文件头补 `from __future__ import annotations`，清理导入顺序。
- 常量 `PROJECT_ROOT`、`DATA_ROOT`、`ENV_FILE`、`CONFIG_FILE`、`SCHEMA_FILE` 补充 `Path` 类型。
- `_load_config` 已有 `dict[str, Any]` 返回注解，保留该形态；明确只有 Codex 将存储路径相对配置文件解析，MCP 保留原值。
- 保留双包条件导入以兼容 `fersk_mcp`，但将差异集中到一个私有函数。
- 保留模块导入时加载的 `CONFIG` 对象，不引入重载入口或别名对象替换。
- 保留现有对外文案和日志字段，说明文字使用简体中文。

### `validation.py`

- 文件头补 `from __future__ import annotations`。
- `load_config(file_path: str | Path, schema_path: str | Path) -> dict[str, Any]` 补齐类型。
- 保持当前 `RuntimeError` 契约、错误文本及异常链；新增 `ConfigValidationError` 不属于默认风格调整。
- 将有限数值递归检查拆成 `_ensure_finite_numbers`。
- Schema 边界先核对 `OSError`、`UnicodeDecodeError`、`json.JSONDecodeError`、`jsonschema.exceptions.SchemaError` 及非法顶层数据类型的实际失败路径，再评估收窄；不改变当前统一 RuntimeError 的外部行为。
- 将命令行空白、消息类型划分、音频限制等规则拆成小型 helper。
- 保留现有对外文案和日志字段，说明文字使用简体中文；不泄露敏感配置值。

### `initialization.py`

- 文件头补 `from __future__ import annotations`。
- `initialize(default_file: str | Path, target: str | Path) -> None` 明确类型。
- 当前已有临时文件 try/finally 和 FileExistsError 处理；保留硬链接原子发布、fsync、并发不覆盖和失败清理，不将其他 OSError 吞掉。
- 启动脚本逻辑提取为 `main() -> None`，保留现有对外文案和日志字段，说明文字使用简体中文。

## 跨包约束与验收

`../fersk_mcp/configs/{loader,validation,initialization}.py` 以及两份 JSON 配置实际是指向本项目的符号链接。因此修改目标文件会影响两个服务，不属于 Codex 单侧内部整理。不得引入 MCP 未声明的 SDK/Pydantic 运行依赖，不改链接或 JSON。

重点回归：`test_config.py`、`test_shared_config.py`、`test_package_paths.py`。覆盖两侧包名导入、Codex/MCP 路径差异、导入失败时机、错误文本/异常链、有限数值和 Schema 规则、并发初始化不覆盖，以及不读取生产配置。
