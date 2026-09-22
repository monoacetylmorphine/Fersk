"""飞书同步请求统一入口；与模型及文件处理默认线程池隔离。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


# 同一源码由两个包分别加载；配置和线程池始终归属各自服务。
if __package__ == "fersk_mcp.services.lark":
    from fersk_mcp.utils.bounded_executor import BoundedExecutor
    from fersk_mcp.configs.loader import CONFIG
else:
    from fersk_codex.utils.bounded_executor import BoundedExecutor
    from fersk_codex.configs.loader import CONFIG

executor: BoundedExecutor = BoundedExecutor(CONFIG["lark"].get("requestConcurrency", 8))


async def call_lark(operation: Callable[..., T], *args: Any) -> T:
    return await executor.call(operation, *args,
                               timeout=CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])
