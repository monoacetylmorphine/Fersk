"""飞书同步请求统一入口；与模型及文件处理默认线程池隔离。"""

from fersk_codex.utils.bounded_executor import BoundedExecutor
from fersk_codex.utils.config_loader import CONFIG

executor = BoundedExecutor(CONFIG["lark"].get("requestConcurrency", 8))


async def call_lark(operation, *args):
    return await executor.call(operation, *args,
                               timeout=CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])
