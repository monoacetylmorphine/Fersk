"""统一业务日志的控制台输出。"""

from __future__ import annotations

import logging
import re


_logger = logging.getLogger("fersk.codex")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(source)s] [%(asctime)s] [%(levelname)s] %(message)s",
        defaults={"source": "APP"},
    ))
    _logger.addHandler(_handler)
_handler = _logger.handlers[0]
_logger.propagate = False


class RedactingFormatter(logging.Formatter):
    """保留 SDK DEBUG 与原始格式，仅遮蔽连接凭据。"""

    def __init__(self, original: logging.Formatter | None) -> None:
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return re.sub(r"(?i)([?&](?:access_key|ticket|access_token)=)[^&\s]+",
                      r"\1[REDACTED]", self.original.format(record))


def protect_sdk_logs(sdk_logger: logging.Logger) -> None:
    for handler in sdk_logger.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))


def configure_logging(level: str) -> None:
    """由启动入口配置业务等级，不改动 SDK 等级。"""
    _logger.setLevel(level.upper())
    for handler in _logger.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))


def get_logger(source: str) -> logging.LoggerAdapter:
    """为共享 Logger 绑定固定来源，避免并发调用互相覆盖标识。"""
    return logging.LoggerAdapter(_logger, {"source": source})
