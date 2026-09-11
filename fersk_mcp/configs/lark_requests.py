"""MCP 进程独立的有界飞书请求入口。"""

from fersk_mcp.utils.bounded_executor import BoundedExecutor
from fersk_mcp.utils.config_loader import CONFIG

executor = BoundedExecutor(CONFIG["lark"].get("requestConcurrency", 8))


async def call_lark(operation, *args):
    return await executor.call(operation, *args,
                               timeout=CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])
