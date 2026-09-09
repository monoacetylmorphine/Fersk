"""业务日志的统一控制台输出。"""

import logging


_logger = logging.getLogger("fersk")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(source)s] [%(asctime)s] [%(levelname)s] %(message)s",
        defaults={"source": "MCP"},
    ))
    _logger.addHandler(_handler)
_logger.setLevel(logging.INFO)
_logger.propagate = False


def get_logger(source: str) -> logging.LoggerAdapter:
    """为共享 Logger 绑定固定来源，避免并发调用互相覆盖标识。"""
    return logging.LoggerAdapter(_logger, {"source": source})
