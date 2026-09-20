"""会话恢复、重置以及历史名称和时间同步。"""

import asyncio

from fersk_codex.codex import thread_manager
from fersk_codex.codex.thread_watchdog import settings
from fersk_codex.session import session_history
from fersk_codex.utils.logger import get_logger

logger = get_logger("Codex")


async def _initialize_session_name(user_id: str, thread, prompt: str | list) -> None:
    """仅初始化已登记的新线程；旧版本线程不拿当前输入补造首次名称。"""
    record = await session_history.get_session(user_id, thread.id)
    if record is None:
        return
    if record.thread_name is None:
        record = await session_history.claim_session_name(user_id, thread.id, prompt)
    if record.updated_at is not None:
        return
    metadata = (await thread.read(include_turns=False)).thread
    if record.thread_name is not None and metadata.name != record.thread_name:
        await thread.set_name(record.thread_name)
        metadata = (await thread.read(include_turns=False)).thread
        if metadata.name != record.thread_name:
            raise RuntimeError("线程名称未成功保存")
    await session_history.update_session_time(user_id, thread.id, metadata.updated_at)


async def _sync_session_time(user_id: str, thread) -> None:
    """元数据同步失败单独记录，不把已完成的模型任务改判为执行失败。"""
    try:
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            record = await session_history.get_session(user_id, thread.id)
            if record is not None:
                # steer 命名失败后，不能用时间同步误标为命名已完成。
                if record.thread_name is not None and record.updated_at is None:
                    logger.warning("历史会话命名尚未完成: user_id=%s, thread_id=%s", user_id, thread.id)
                    return
                metadata = (await thread.read(include_turns=False)).thread
                await session_history.update_session_time(user_id, thread.id, metadata.updated_at)
    except Exception:
        logger.exception("历史会话时间同步失败: user_id=%s, thread_id=%s", user_id, thread.id)


class CodexSession:
    """由 FerskCodex 组合 SDK 生命周期及共享运行状态。"""

    @classmethod
    async def restore_session(cls, user_id: str, thread_id: str) -> None:
        """恢复历史并替换活跃绑定；调用方必须先停止任务并持有提交锁。"""
        if await session_history.get_session(user_id, thread_id) is None:
            raise ValueError("历史会话不存在或不属于当前用户")
        if await thread_manager.get_user_thread(user_id) == thread_id:
            return
        async with cls._session(None) as codex:
            thread = await codex.thread_unarchive(thread_id)
            await _initialize_session_name(user_id, thread, "")
            metadata = (await thread.read(include_turns=False)).thread
        # SDK 操作与关闭成功后才写数据库；不归档原线程，不创建替代线程。
        await session_history.update_session_time(user_id, thread_id, metadata.updated_at)
        await thread_manager.set_user_thread(user_id, thread_id)

    @classmethod
    async def reset_thread(cls, user_id: str) -> None:
        """Explicit control operation; prompt text never resets a thread."""
        from fersk_codex.codex.codex_execution import _retry_on_overload_async

        thread_id = await thread_manager.get_user_thread(user_id)
        if thread_id:
            async with cls._session(None) as codex:
                await _retry_on_overload_async(
                    lambda: codex.thread_archive(thread_id=thread_id),
                    operation_name="thread_archive",
                )
        await thread_manager.set_user_thread(user_id, None)
