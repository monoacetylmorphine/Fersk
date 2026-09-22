"""消息批次执行、steer 转交和流式卡片交付。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import replace
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from fersk_codex.codex.thread_watchdog import RunProbe, probes, settings
from fersk_codex.services.lark.lark_message_card import (
    CardDeliveryError, CardReplace, CardStreamSession, CardStreamStopped,
)
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger
from .message_assemble import InputAssemblyError
from .message_collector import MessageBatch
from fersk_codex.session.session_gateway import ActiveCodexRun
from .gateway_runtime import GatewayRuntime

if TYPE_CHECKING:
    from fersk_codex.codex.codex_execution import RunEvent

    from fersk_codex.middleware.message_assemble import AssemblyResult, CodexRunInput
    from fersk_codex.services.lark.lark_message_card import CardSteer

logger = get_logger("Message")


class GatewayExecution:
    """通过共享 runtime 协调提交和收尾，输出阶段不占用提交锁。"""

    def __init__(
        self,
        runtime: GatewayRuntime,
        *,
        assemble_input: Callable[[MessageBatch], Awaitable[AssemblyResult]],
    ) -> None:
        self.runtime = runtime
        self.cache = runtime.cache
        self.assemble_input = assemble_input

    def _pending_batch(self, batch: MessageBatch) -> MessageBatch:
        """在原有调用位置筛除已处理消息，不改变提交锁的边界。"""
        return replace(batch, messages=tuple(
            message for message in batch.messages
            if message.message_id not in self.cache.processed_message_ids.get(batch.chat_id, {})
            and message.message_id not in self.cache.active_runs_by_message_id
        ))

    async def _handle_message_batch(self, batch: MessageBatch, generation: int) -> None:
        if len(self.cache.all_runs) + len(self.runtime.detached_tasks) >= CONFIG["messaging"].get("maxPendingEvents", 32):
            try:
                await self.runtime.send_card(batch.union_id, "当前任务繁忙，请稍后重试。")
            finally:
                await self.runtime._clear_reaction(batch.chat_id, {m.message_id for m in batch.messages})
            return
        # History may contain a completed/active run; it must not backdate this new run's deadline.
        batch = self._pending_batch(batch)
        if not batch.messages:
            return
        state = ActiveCodexRun(uuid4().hex, batch.chat_id,
                               frozenset(m.message_id for m in batch.messages), target_id=batch.union_id)
        if batch.chat_id in self.cache.blocked_chats:
            try:
                await self.runtime._notify_terminal(state, "stopFailed")
            finally:
                await self.runtime._clear_reaction(batch.chat_id, state.message_ids)
            return
        state.probe = RunProbe(state.run_id, state.chat_id, state.message_ids,
                              received_at=min((self.cache.received_at.get(mid, time.monotonic())
                                               for mid in state.message_ids), default=time.monotonic()))
        probes[state.run_id] = state.probe
        self.cache.all_runs[state.run_id] = state
        state.probe.record("received")
        state.task = asyncio.create_task(self._execute_message_batch(batch, generation, state))
        watcher = asyncio.create_task(self.runtime._watch_run(state))
        try:
            done, _ = await asyncio.wait({state.task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if state.task in done:
                await state.task
            elif not watcher.cancelled():
                watcher.result()
        except asyncio.CancelledError:
            if not state.interrupted:
                state.interrupted = True
                state.probe.stop_reason = "shutdown"
                stopping = asyncio.create_task(self.runtime._interrupt_run(state))
                self.runtime._observe_detached(stopping)
                await asyncio.wait({stopping}, timeout=settings()["interruptGraceSeconds"])
                raise
        finally:
            if state.stop_task:
                await asyncio.wait({state.stop_task}, timeout=settings()["cleanupTimeoutSeconds"])
            if state.probe.cleanup_timed_out or (state.probe.stop_reason and state.probe.stop_reason.endswith("timeout")):
                # worker 先完成时，给 watchdog 已发起的超时通知保留请求预算。
                await asyncio.wait({watcher}, timeout=settings()["cardRequestTimeoutSeconds"] + settings()["cleanupTimeoutSeconds"])
            watcher.cancel()
            await asyncio.wait({watcher}, timeout=settings()["cleanupTimeoutSeconds"])
            if not watcher.done():
                self.runtime._observe_detached(watcher)
            for pending in (state.stop_task, state.timeout_task):
                if pending is not None and not pending.done():
                    pending.cancel()
                    self.runtime._observe_detached(pending)
            if state.task is not None and not state.task.done() and not state.released:
                await self.runtime._cleanup_timeout(state)
            self.runtime._release_run(state)
            # 输出 worker 退出、卡片收尾后清除；steer 消息继续由接收任务负责。
            if state.task is None or state.task.done():
                try:
                    async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                        await self.runtime._clear_reaction(batch.chat_id, {
                            mid for mid in state.message_ids
                            if self.cache.active_runs_by_message_id.get(mid) in (None, state)
                        })
                except Exception:
                    logger.exception("任务 reaction 清理未完成，交由后台重试: run_id=%s", state.run_id)

    async def _execute_message_batch(
        self,
        batch: MessageBatch,
        generation: int,
        state: ActiveCodexRun,
    ) -> None:
        """Serialize input submission only; the original task owns the reply stream."""
        events = None
        transferred = False
        try:
            async with self.runtime._submission_lock(batch.chat_id):
                if generation != self.cache.chat_generations.get(batch.chat_id, 0):
                    return
                batch = self._pending_batch(batch)
                if not batch.messages:
                    return
                for message in batch.messages:
                    self.runtime._remember_message(self.cache.processed_message_ids, batch.chat_id, message.message_id)
                state = await self.runtime._register_active_run(batch, state)
                state.probe.message_ids = state.message_ids
                state.probe.stage("preparing")
                if await self.runtime._run_was_interrupted(state):
                    return
                try:
                    assembly = await self.assemble_input(batch)
                except InputAssemblyError as exc:
                    state.probe.finish("failed")
                    if not await self.runtime._run_was_interrupted(state):
                        await self.runtime.send_card(
                            union_id=batch.union_id,
                            content=exc.user_message,
                        )
                    return

                for notice in assembly.notices:
                    if await self.runtime._run_was_interrupted(state):
                        return
                    await self.runtime.send_card(union_id=batch.union_id, content=notice)

                if assembly.codex_input is None or await self.runtime._run_was_interrupted(state):
                    return
                logger.info("Received : user_id=%s, prompt=%s", batch.union_id, assembly.codex_input)
                owner = self.cache.active_runs_by_chat.get(batch.chat_id)
                if batch.chat_id in self.cache.blocked_chats:
                    await self.runtime._notify_terminal(state, "stopFailed")
                    return
                if owner is not None:
                    async with owner.controls, owner.cards.steering(CONFIG["messages"]["steerAccepted"]) as rotation:
                        if not owner.interrupted:
                            result = await self.runtime.codex.steer(
                                owner.run_id, assembly.codex_input,
                                cancelled=lambda: state.interrupted,
                            )
                            if result["type"] == "cancelled":
                                return
                            if result["type"] == "steered":
                                async with self.cache.active_runs_guard:
                                    owner.message_ids |= state.message_ids
                                    for message_id in state.message_ids:
                                        self.cache.active_runs_by_message_id[message_id] = owner
                                    if state.interrupted:
                                        owner.interrupted = True
                                transferred = True
                                state.probe.finish("steered")
                                if owner.probe:
                                    owner.probe.message_ids = owner.message_ids
                                    owner.probe.record("steered", sourceRunId=state.run_id)
                                if owner.interrupted:
                                    await self.runtime._interrupt_run(owner)
                                else:
                                    rotation.decide(True)
                                    delivered = await asyncio.shield(rotation.applied)
                                    if owner.probe:
                                        owner.probe.record("steer_card_rotated", delivered=delivered,
                                                           sourceRunId=state.run_id)
                                return
                            if result["type"] == "error":
                                if not await self.runtime._run_was_interrupted(state):
                                    await self.runtime.send_card(batch.union_id, result["content"])
                                return
                    # Idle means the server finished, but the original stream may
                    # still be draining. Interrupted runs also wait here.
                    await owner.finished.wait()

                if batch.chat_id in self.cache.blocked_chats:
                    await self.runtime._notify_terminal(state, "stopFailed")
                    return

                if generation != self.cache.chat_generations.get(batch.chat_id, 0) or not await self.runtime._start_codex_run(state):
                    return
                state.probe.stage("starting")
                events = self.runtime.codex.running(
                    user_id=batch.union_id, prompt=assembly.codex_input,
                    run_id=state.run_id, notify_started=True,
                )
                first = await anext(events, None)
                if first is None:
                    raise RuntimeError("Codex 启动后未返回任何状态")
                if first["type"] != "started":
                    state.probe.finish("failed" if first["type"] == "error" else "completed")
                    if not await self.runtime._run_was_interrupted(state):
                        await self.runtime.send_card(batch.union_id, first.get("content", ""))
                    return
                state.cards = CardStreamSession(cancelled=lambda: state.interrupted)
                self.cache.active_runs_by_chat[batch.chat_id] = state
                state.probe.thread_id = first.get("thread_id")
                state.probe.turn_id = first.get("turn_id")
                state.probe.stage("running")
            # No submission lock is held during model output or CardKit updates.
            await self._deliver_reply(batch, assembly.codex_input, state, events)
        except Exception as exc:
            logger.exception("消息批次处理异常: chat_id=%s, error=%s", batch.chat_id, exc)
            state.probe.record("exception", errorType=type(exc).__name__)
            if not state.interrupted:
                state.probe.finish("failed")
                state.interrupted = True
                state.probe.stop_reason = "failed"
                await self.runtime._interrupt_run(state)
                await self.runtime._notify_terminal(state, "codexFailure")
        finally:
            if state is not None:
                if state.cards is not None:
                    state.cards.close()
                try:
                    try:
                        if events is not None:
                            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                                await events.aclose()
                    except Exception:
                        logger.exception("事件流清理失败: run_id=%s", state.run_id)
                        if not await self.runtime.codex.force_close(state.run_id) and not state.expired:
                            self.cache.blocked_chats[state.chat_id] = state
                            state.probe.stop_confirmed = False
                            state.probe.record("cleanup_unconfirmed")
                    finally:
                        try:
                            async with asyncio.timeout(settings()["interruptGraceSeconds"] + settings()["cleanupTimeoutSeconds"]):
                                if state.stop_task:
                                    await asyncio.shield(state.stop_task)
                                async with state.controls:
                                    if not transferred:
                                        await self.runtime._unregister_active_run(state)
                                    if self.cache.active_runs_by_chat.get(batch.chat_id) is state:
                                        self.cache.active_runs_by_chat.pop(batch.chat_id, None)
                                    await self.runtime.codex.forget_run(state.run_id)
                        except Exception:
                            logger.exception("任务收尾失败: run_id=%s", state.run_id)
                            if not transferred:
                                await self.runtime._unregister_active_run(state)
                            if self.cache.active_runs_by_chat.get(batch.chat_id) is state:
                                self.cache.active_runs_by_chat.pop(batch.chat_id, None)
                finally:
                    state.finished.set()

    async def _deliver_reply(
        self,
        batch: MessageBatch,
        codex_input: CodexRunInput,
        state: ActiveCodexRun,
        events: AsyncGenerator[RunEvent, None],
    ) -> None:
        """模型流由当前任务持有，卡片交付失败仍继续完成模型收尾。"""
        async with aclosing(state.cards.events(events)) as output_events, aclosing(
            self._reply_content(batch, codex_input, state, events=output_events)
        ) as content:
            try:
                await self.runtime.send_card(union_id=batch.union_id, content=content, session=state.cards)
            except CardDeliveryError as exc:
                # The sender drains the model stream before reporting delivery failure.
                # A CardKit outage must not interrupt an otherwise healthy Codex turn.
                logger.error("卡片交付失败: run_id=%s, error_type=%s", state.run_id, type(exc).__name__)
                state.probe.record("delivery_failed", errorType=type(exc).__name__)

    async def _reply_content(
        self,
        batch: MessageBatch,
        codex_input: CodexRunInput,
        state: ActiveCodexRun,
        *,
        events: AsyncGenerator[RunEvent, None] | None = None,
    ) -> AsyncGenerator[str | CardReplace | CardSteer, None]:
        """展示推理、工具及运行进度；最终答案替换正文并保持定格。"""
        answer_started = False
        last_text_item = None
        async with aclosing(events if events is not None else self.runtime.codex.running(
            user_id=batch.union_id, prompt=codex_input, run_id=state.run_id,
        )) as events:
            async for event in events:
                if await self.runtime._run_was_interrupted(state):
                    raise CardStreamStopped()
                event_type = event.get("type")
                chunk = event.get("content", "")
                if event_type == "card_control":
                    rotation = event["control"]
                    yield rotation
                    if rotation.accepted:
                        answer_started = False
                        last_text_item = None
                elif event_type in {"reasoning", "progress", "usage"} or (
                    event_type == "answer" and event.get("phase") == "commentary"
                ):
                    if chunk and not answer_started:
                        text_item = (event_type, event.get("item_id"))
                        separator = "\n\n" if last_text_item is not None and last_text_item != text_item else ""
                        yield separator + chunk
                        last_text_item = text_item
                        answer_started = False
                elif event_type in {"answer", "cmd"}:
                    if chunk:
                        if not answer_started:
                            answer_started = True
                            yield CardReplace(chunk)
                        else:
                            yield chunk
                        last_text_item = (event_type, event.get("item_id"))
                elif event_type == "error":
                    message = event.get("content", CONFIG["messages"]["codexFailure"])
                    yield "\n\n" + message if answer_started else CardReplace(message)
                    answer_started = True
                    if getattr(state, "probe", None):
                        state.probe.finish("failed")
                elif event_type == "interrupted":
                    yield CardReplace(CONFIG["messages"]["taskInterrupted"])
                elif event_type == "done" and getattr(state, "probe", None):
                    state.probe.finish("completed")
        if await self.runtime._run_was_interrupted(state):
            raise CardStreamStopped()
