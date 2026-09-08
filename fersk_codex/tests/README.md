# 测试

在已安装项目 `requirements.txt` 依赖的 Python 3.13+ 环境中，从项目根目录运行：

```sh
python -B tests/run_tests.py
python -B tests/run_tests.py --pattern test_message_assemble.py -v
python -B tests/run_tests.py --reverse
```

测试入口使用 `configs/config_default.json` 生成临时配置，将数据库、工作区、CSV 和运行日志定向到临时目录，不加载运行环境的 `.env`。不需要启动飞书、Codex 或语音识别服务。原有进程清理测试会启动短暂的本地 Python 子进程。

`--reverse` 反向执行测试，用于检查模块缓存、全局状态和 mock 是否影响其他用例。未匹配任何测试或测试失败时返回非零退出码。仍可使用原来的 `unittest discover -s tests`，但该方式需要自行设置包导入路径和 `FERSK_CONFIG_FILE`。

新增覆盖包括：资源签名/MIME/文件名校验，历史排序与消息边界，富文本和多模态组装，下载与转写失败、取消清理，音频分段边界，配置错误，飞书 SDK 请求参数，SQLite/CSV 持久化以及重试退避。
