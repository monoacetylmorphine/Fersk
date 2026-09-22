"""在线程回调创建协程前限制容量，并消费处理 Future 的异常。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import CancelledError, Future
from typing import Any, TypeVar

from fersk_codex.utils.logger import get_logger

EventT = TypeVar("EventT")

logger = get_logger("Event")


class EventDispatcher:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        capacity: int = 32,
        controls: int = 4,
    ) -> None:
        # 来源：内部小规模部署的初始保护值，控制事件独立预留容量。
        self.loop = loop
        self.normal = threading.BoundedSemaphore(capacity)
        self.controls = threading.BoundedSemaphore(controls)

    def submit(
        self,
        handler: Callable[[EventT], Coroutine[Any, Any, None]],
        data: EventT,
        *,
        control: bool = False,
    ) -> bool:
        slots = self.controls if control else self.normal
        if not slots.acquire(blocking=False):
            logger.error("入站事件容量已满: control=%s", control)
            return False
        coroutine = None
        try:
            coroutine = handler(data)
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            if coroutine is not None:
                coroutine.close()
            slots.release()
            raise
        def done(completed: Future[None]) -> None:
            slots.release()
            try:
                completed.result()
            except CancelledError:
                pass
            except Exception:
                logger.exception("入站事件处理失败: control=%s", control)
        future.add_done_callback(done)
        return True
