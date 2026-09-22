"""网关共享状态、提交锁、运行监督、停止确认和后台资源清理。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from fersk_codex.codex.thread_watchdog import probes, settings
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger
from .message_collector import MessageBatch
from fersk_codex.session.session_gateway import ActiveCodexRun, SessionCache

if TYPE_CHECKING:
    from fersk_codex.codex.codex_execution import FerskCodex

logger = get_logger("Message")


class GatewayRuntime:
    """所有网关组件共享同一缓存和外部服务依赖。"""

    def __init__(
        self,
        cache: SessionCache,
        *,
        codex: type[FerskCodex],
        send_card: Callable[..., Awaitable[str | None]],
        delete_reaction: Callable[..., Awaitable[bool]],
    ) -> None:
        self.cache = cache
        self.codex = codex
        self.send_card = send_card
        self.delete_reaction = delete_reaction
        self.detached_tasks: set[asyncio.Task[Any]] = set()

    async def maintain(self, operation: Callable[[], Awaitable[None]]) -> None:
        """按既有检查间隔重试清理，单轮失败不终止维护任务。"""
        while True:
            await asyncio.sleep(settings()["checkIntervalSeconds"])
            try:
                await operation()
            except Exception:
                logger.exception("后台清理失败")

    def _observe_detached(self, task: asyncio.Task[Any]) -> None:
        """保留残留任务引用并消费异常；不等待不配合取消的协程。"""
        if task in self.detached_tasks:
            return
        self.detached_tasks.add(task)
        def done(completed: asyncio.Task[Any]) -> None:
            self.detached_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error("后台收尾任务失败", exc_info=completed.exception())
        task.add_done_callback(done)

    def _release_run(self, state: ActiveCodexRun) -> None:
        """事件循环内无 await 的幂等释放，不能依赖网络或异步锁。"""
        if state.released:
            return
        state.released = True
        for mid, owner in list(self.cache.active_runs_by_message_id.items()):
            if owner is state:
                self.cache.active_runs_by_message_id.pop(mid, None)
        if self.cache.active_runs_by_chat.get(state.chat_id) is state:
            self.cache.active_runs_by_chat.pop(state.chat_id, None)
        self.cache.all_runs.pop(state.run_id, None)
        state.finished.set()
        state.probe.finish(state.probe.stop_reason or "completed")
        state.probe.record("released", cleanupTimedOut=state.probe.cleanup_timed_out)
        logger.info("任务已释放: run_id=%s, terminal=%s, cleanup_timeout=%s",
                    state.run_id, state.probe.terminal, state.probe.cleanup_timed_out)
        if self.cache.blocked_chats.get(state.chat_id) is not state:
            probes.pop(state.run_id, None)
        # 未完成的 worker 仍可能使用 cards/task；不提前清空引用。
        if state.task is None or state.task.done():
            self.cache.finish_run(state)
        else:
            state.task.add_done_callback(lambda _: self.cache.finish_run(state))

    async def _cleanup_timeout(self, state: ActiveCodexRun) -> None:
        """收尾期限耗尽后，有界尝试关闭进程并唤醒等待者。"""
        if state.released:
            return
        state.probe.cleanup_timed_out = True
        state.probe.record("cleanup_timeout")
        logger.error("任务收尾超时: run_id=%s, terminal=%s", state.run_id, state.probe.terminal)
        state.interrupted = True
        if state.cards is not None:
            state.cards.close()
        if state.task is not None and not state.task.done():
            state.task.cancel()
        closing = asyncio.create_task(self.codex.force_close(state.run_id))
        self._observe_detached(closing)
        confirmed = False
        try:
            pending = {closing}
            if state.task is not None:
                pending.add(state.task)
            # 进程关闭与 worker 取消共享同一宽限；不能把尚未获得调度的 worker 误判为残留。
            done, _ = await asyncio.wait(pending, timeout=settings()["cleanupTimeoutSeconds"])
            if closing in done and not closing.cancelled():
                confirmed = bool(closing.result())
        except Exception:
            logger.exception("强制关闭失败: run_id=%s", state.run_id)
        finally:
            if not closing.done():
                closing.cancel()
            state.detached = state.task is not None and not state.task.done()
            if state.detached:
                self._observe_detached(state.task)
            # 残留 worker 可能仍持有提交锁，不能允许新运行与它重叠。
            if not confirmed or state.detached:
                self.cache.blocked_chats[state.chat_id] = state
            self._release_run(state)
        await self._notify_terminal(state, "cleanupTimeout")

    def _remember_message(
        self,
        journal: dict[str, dict[str, float | None]],
        chat_id: str,
        message_id: str,
    ) -> None:
        self.cache.remember(journal, chat_id, message_id)

    async def _register_active_run(
        self,
        batch: MessageBatch,
        state: ActiveCodexRun | None = None,
    ) -> ActiveCodexRun:
        state = state or ActiveCodexRun(
            run_id=uuid4().hex,
            chat_id=batch.chat_id,
            message_ids=frozenset(message.message_id for message in batch.messages),
        )
        state.message_ids = frozenset(message.message_id for message in batch.messages)
        async with self.cache.active_runs_guard:
            state.interrupted |= bool(state.message_ids & self.cache.recalled_message_ids)
            for message_id in state.message_ids:
                self.cache.active_runs_by_message_id[message_id] = state
                self.cache.recalled_message_ids.discard(message_id)
        return state

    async def _unregister_active_run(self, state: ActiveCodexRun) -> None:
        async with self.cache.active_runs_guard:
            for message_id in state.message_ids:
                if self.cache.active_runs_by_message_id.get(message_id) is state:
                    self.cache.active_runs_by_message_id.pop(message_id, None)

    async def _run_was_interrupted(self, state: ActiveCodexRun) -> bool:
        async with self.cache.active_runs_guard:
            return state.interrupted

    async def _start_codex_run(self, state: ActiveCodexRun) -> bool:
        """Atomically open the turn-start window unless recall already won."""
        async with self.cache.active_runs_guard:
            if state.interrupted:
                return False
            state.codex_started = True
            return True

    async def _get_codex_lock(self, chat_id: str) -> asyncio.Lock:
        async with self.cache.codex_locks_guard:
            return self.cache.codex_locks.setdefault(chat_id, asyncio.Lock())

    @asynccontextmanager
    async def _submission_lock(self, chat_id: str) -> AsyncIterator[None]:
        with self.cache.hold(chat_id):
            lock = await self._get_codex_lock(chat_id)
            while True:
                reset = self.cache.reset_tasks.get(chat_id)
                if reset is not None:
                    await asyncio.shield(reset)
                await lock.acquire()
                if self.cache.reset_tasks.get(chat_id) is None:
                    break
                lock.release()
            try:
                yield
            finally:
                lock.release()

    async def _notify_terminal(self, state: ActiveCodexRun, key: str) -> None:
        if state.notified:
            return
        state.notified = True
        try:
            async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                content = (CONFIG["messages"].get("cleanupTimeout", "任务收尾超时，交付结果未确认；如后续任务被阻止，请使用 /stop 重试停止。")
                           if key == "cleanupTimeout" else CONFIG["messages"][key])
                await self.send_card(state.target_id or state.chat_id, content)
            if state.probe:
                state.probe.record("notification_sent", messageKey=key)
        except Exception:
            # Delivery may have succeeded despite a lost response; don't resend blindly.
            logger.exception("最终卡片投递失败或结果不确定: run_id=%s", state.run_id)
            if state.probe:
                state.probe.record("notification_uncertain", messageKey=key)

    async def _watch_run(self, state: ActiveCodexRun) -> None:
        async def stop_and_notify() -> None:
            confirmed = await self._interrupt_run(state)
            await self._notify_terminal(state, "taskTimeout" if confirmed else "taskTimeoutUnconfirmed")

        while not state.finished.is_set():
            await asyncio.sleep(settings()["checkIntervalSeconds"])
            if state.finished.is_set():
                break
            probe = state.probe
            reason = probe.expired()
            if reason == "cleanup_timeout":
                await self._cleanup_timeout(state)
                return
            if reason:
                if state.codex_started:
                    status = await self.codex.completed_status(state.run_id)
                    if status:
                        probe.finish(status)
                        continue
                    # Completion or a user stop may have won during the status RPC.
                    if probe.terminal or probe.stop_reason:
                        continue
                state.interrupted = True
                probe.stop_reason = reason
                probe.begin_cleanup()
                state.timeout_task = asyncio.create_task(stop_and_notify())
                self._observe_detached(state.timeout_task)
                # 停止请求发出后仍监督收尾，不能在 worker 释放前退出。
        if state.timeout_task is not None:
            await asyncio.wait({state.timeout_task}, timeout=settings()["cardRequestTimeoutSeconds"])

    async def _clear_reaction(
        self,
        chat_id: str,
        message_ids: frozenset[str] | set[str] | None = None,
    ) -> None:
        # 固定本次清理范围；网络请求期间新到达的消息留给所属批次处理。
        keys = []
        for message_id, reaction_id in list(self.cache.reaction_message_ids.get(chat_id, {}).items()):
            if message_ids is not None and message_id not in message_ids:
                continue
            owner = self.cache.active_runs_by_message_id.get(message_id)
            if owner is not None and owner.task is not None and not owner.task.done():
                continue
            key = (chat_id, message_id, reaction_id)
            self.cache.queue_reaction(*key)
            keys.append(key)
        # 首次 await 前登记全部记录，取消也不会丢失未尝试的删除。
        for key in keys:
            await self._delete_pending_reaction(key)

    async def _delete_pending_reaction(self, key: tuple[str, str, str]) -> None:
        pending = self.cache.pending_reactions.get(key)
        if pending is None or key in self.cache.reactions_being_cleared:
            return
        chat_id, message_id, reaction_id = key
        self.cache.reactions_being_cleared.add(key)
        try:
            async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                succeeded = await self.delete_reaction(message_id=message_id, reaction_id=reaction_id)
            if succeeded:
                self.cache.pending_reactions.pop(key, None)
                current = self.cache.reaction_message_ids.get(chat_id, {})
                if current.get(message_id) == reaction_id:
                    current.pop(message_id)
                    self.cache._reaction_times.pop((chat_id, message_id), None)
                    if not current:
                        self.cache.reaction_message_ids.pop(chat_id, None)
                return
        except Exception:
            logger.exception("清理消息 reaction 异常: chat_id=%s", chat_id)
        finally:
            self.cache.reactions_being_cleared.discard(key)
            if self.cache.pending_reactions.get(key) is pending:
                # 来源：本次修复约定的退避策略，非飞书平台限制。
                delays = (5, 30, 120, 600)
                pending.due = time.monotonic() + delays[min(pending.attempts, len(delays) - 1)]
                pending.attempts += 1

    async def _retry_reactions(self) -> None:
        # 每轮最多处理一个，避免失败重试挤占正常消息请求。
        for key, pending in list(self.cache.pending_reactions.items()):
            if pending.due <= time.monotonic():
                await self._delete_pending_reaction(key)
                return

    async def _interrupt_run(self, state: ActiveCodexRun) -> bool:
        """One stop operation per run. A failed confirmation can be retried by /stop."""
        caller = asyncio.current_task()

        async def stop() -> bool:
            confirmed = not state.codex_started
            if state.probe:
                state.probe.begin_cleanup()
                state.probe.stage("stopping")
            try:
                if state.codex_started:
                    confirmed = await self.codex.interrupt_and_confirm(state.run_id)
            except Exception:
                logger.exception("停止任务失败: run_id=%s", state.run_id)
                confirmed = False
            if state.detached and state.task is not None and not state.task.done():
                confirmed = False
            if state.probe:
                state.probe.stop_confirmed = confirmed
                state.probe.finish(state.probe.stop_reason or "stopped")
                state.probe.record("stop_result")
            if confirmed:
                if self.cache.blocked_chats.get(state.chat_id) is state:
                    self.cache.blocked_chats.pop(state.chat_id, None)
                    if state.finished.is_set():
                        probes.pop(state.run_id, None)
            elif not state.expired:
                self.cache.blocked_chats[state.chat_id] = state
            if state.task is not None and state.task is not caller and not state.task.done():
                state.task.cancel()
            return confirmed

        if state.stop_task is None:
            state.stop_task = asyncio.create_task(stop())
        return await asyncio.shield(state.stop_task)

    async def _expire_session_cache(self) -> None:
        for state in self.cache.prune():
            state.expired = True
            state.interrupted = True
            task = state.task
            if task is not None and not task.done():
                task.cancel()
            try:
                await self.codex.discard_expired_run(state.run_id)
            except Exception:
                logger.exception("过期任务关闭失败，仍按 24 小时期限清理缓存: run_id=%s", state.run_id)
            finally:
                try:
                    if task is not None and not task.done():
                        try:
                            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                                await asyncio.shield(task)
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                        except Exception:
                            logger.exception("过期任务收尾未完成: run_id=%s", state.run_id)
                finally:
                    self.cache.expire_run(state)
                    probes.pop(state.run_id, None)
                    logger.warning("已清理超过 24 小时的任务缓存: run_id=%s", state.run_id)
