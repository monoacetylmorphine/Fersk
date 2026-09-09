# Fersk Codex

主项目通过飞书消息驱动 Codex，持久化配置与工作区从宿主机 `~/.fersk`、`~/.codex` 挂载读取。部署方式见[根目录说明](../README.md)。

主项目直接使用 `AsyncCodex()`，由你通过 Codex CLI 管理挂载的用户配置；代码不注册扩展或覆盖其连接设置。共享配置中的扩展字段由扩展自行校验，主项目可在没有该字段时独立运行。

在已安装锁定依赖的环境中使用 `python -m fersk_codex.gateway` 启动。离线测试从本项目目录运行 `python -B tests/run_tests.py`。
