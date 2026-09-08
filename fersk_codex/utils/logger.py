"""统一业务日志与 MCP 日志的控制台输出，保留各自的日志级别。"""

import logging


_logger = logging.getLogger("fersk")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(source)s] [%(asctime)s] [%(levelname)s] %(message)s",
        defaults={"source": "MCP"},
    ))
    _logger.addHandler(_handler)
_handler = _logger.handlers[0]
_logger.setLevel(logging.INFO)
_logger.propagate = False


def get_logger(source: str) -> logging.LoggerAdapter:
    """为共享 Logger 绑定固定来源，避免并发调用互相覆盖标识。"""
    return logging.LoggerAdapter(_logger, {"source": source})


def configure_mcp_logging(level: str) -> None:
    """让 MCP 复用输出 Handler，不修改根 Logger 或飞书 SDK 日志。"""
    log_level = level.upper()
    mcp_logger = logging.getLogger("mcp")
    mcp_logger.setLevel(log_level)
    mcp_logger.handlers[:] = [_handler]
    mcp_logger.propagate = False
    for name in ("mcp.server", "mcp.server.streamable_http"):
        logging.getLogger(name).setLevel(log_level)
