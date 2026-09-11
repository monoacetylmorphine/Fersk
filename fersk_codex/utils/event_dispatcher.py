"""在线程回调创建协程前限制容量，并消费处理 Future 的异常。"""

import asyncio
from concurrent.futures import CancelledError
import threading

from fersk_codex.utils.logger import get_logger

logger = get_logger("Event")


class EventDispatcher:
    def __init__(self, loop, capacity=32, controls=4):
        # 来源：内部小规模部署的初始保护值，控制事件独立预留容量。
        self.loop = loop
        self.normal = threading.BoundedSemaphore(capacity)
        self.controls = threading.BoundedSemaphore(controls)

    def submit(self, handler, data, *, control=False):
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
        def done(completed):
            slots.release()
            try:
                completed.result()
            except CancelledError:
                pass
            except Exception:
                logger.exception("入站事件处理失败: control=%s", control)
        future.add_done_callback(done)
        return True
