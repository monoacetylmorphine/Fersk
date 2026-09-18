import asyncio
import sys
import time
from pathlib import Path
from dataclasses import replace
from contextlib import aclosing, asynccontextmanager
from uuid import uuid4

import lark_oapi as lark

# Direct script execution needs the package's parent on the import path.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fersk_codex.core.codex import FerskCodex
from fersk_codex.core.session_history import get_session, list_sessions
from fersk_codex.core.thread_manager import get_user_thread
from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger, configure_logging
from fersk_codex.utils.event_dispatcher import EventDispatcher
from fersk_codex.core.thread_watchdog import RunProbe, probes, settings, journal
from fersk_codex.services.lark.lark_client import create_websocket_client
from fersk_codex.services.lark.lark_tools import getting_chat_history, adding_reaction_emoji, delete_reaction_emoji
from fersk_codex.services.lark.lark_card import CardDeliveryError, CardReplace, CardStreamSession, CardStreamStopped, sending_card
from fersk_codex.services.lark import lark_interactive_card as interactive
from fersk_codex.middleware.message_collector import MessageBatch
from fersk_codex.middleware.message_collector import is_stop_command, is_new_command, is_history_command
from fersk_codex.middleware.session_cache import ActiveCodexRun, SessionCache
from fersk_codex.middleware.message_router import MessageRouter, _bot_identity, _is_bot_mentioned
from fersk_codex.middleware.message_assemble import InputAssemblyError, assemble_codex_input

message_buffer_seconds = CONFIG["messaging"]["bufferWindowSeconds"]

cache = SessionCache()
codex_locks = cache.codex_locks
codex_locks_guard = cache.codex_locks_guard
reset_tasks = cache.reset_tasks
buffered_events = cache.buffered_events
buffer_tasks = cache.buffer_tasks
buffer_guard = cache.buffer_guard
reaction_message_ids = cache.reaction_message_ids
reactions_being_cleared = cache.reactions_being_cleared
recalled_message_ids = cache.recalled_message_ids
chat_generations = cache.chat_generations
pending_chat_requests = cache.pending_chat_requests
received_message_ids = cache.received_message_ids
processed_message_ids = cache.processed_message_ids
received_at = cache.received_at
active_runs_by_message_id = cache.active_runs_by_message_id
active_runs_guard = cache.active_runs_guard
active_runs_by_chat = cache.active_runs_by_chat
all_runs = cache.all_runs
blocked_chats = cache.blocked_chats

logger = get_logger("Message")
detached_tasks = set()
history_cards = interactive.HistoryCardStore()


def _observe_detached(task):
    """保留残留任务引用并消费异常；不等待不配合取消的协程。"""
    if task in detached_tasks:
        return
    detached_tasks.add(task)
    def done(completed):
        detached_tasks.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            logger.error("后台收尾任务失败", exc_info=completed.exception())
    task.add_done_callback(done)


def _release_run(state):
    """事件循环内无 await 的幂等释放，不能依赖网络或异步锁。"""
    if state.released:
        return
    state.released = True
    for mid, owner in list(active_runs_by_message_id.items()):
        if owner is state:
            active_runs_by_message_id.pop(mid, None)
    if active_runs_by_chat.get(state.chat_id) is state:
        active_runs_by_chat.pop(state.chat_id, None)
    all_runs.pop(state.run_id, None)
    state.finished.set()
    state.probe.finish(state.probe.stop_reason or "completed")
    state.probe.record("released", cleanupTimedOut=state.probe.cleanup_timed_out)
    logger.info("任务已释放: run_id=%s, terminal=%s, cleanup_timeout=%s",
                state.run_id, state.probe.terminal, state.probe.cleanup_timed_out)
    if blocked_chats.get(state.chat_id) is not state:
        probes.pop(state.run_id, None)
    # 未完成的 worker 仍可能使用 cards/task；不提前清空引用。
    if state.task is None or state.task.done():
        cache.finish_run(state)
    else:
        state.task.add_done_callback(lambda _: cache.finish_run(state))


async def _cleanup_timeout(state):
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
    closing = asyncio.create_task(FerskCodex.force_close(state.run_id))
    _observe_detached(closing)
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
            _observe_detached(state.task)
        # 残留 worker 可能仍持有提交锁，不能允许新运行与它重叠。
        if not confirmed or state.detached:
            blocked_chats[state.chat_id] = state
        _release_run(state)
    await _notify_terminal(state, "cleanupTimeout")

def _remember_message(journal, chat_id, message_id):
    cache.remember(journal, chat_id, message_id)


async def _register_active_run(batch: MessageBatch, state=None) -> ActiveCodexRun:
    state = state or ActiveCodexRun(
        run_id=uuid4().hex,
        chat_id=batch.chat_id,
        message_ids=frozenset(message.message_id for message in batch.messages),
    )
    state.message_ids = frozenset(message.message_id for message in batch.messages)
    async with active_runs_guard:
        state.interrupted |= bool(state.message_ids & recalled_message_ids)
        for message_id in state.message_ids:
            active_runs_by_message_id[message_id] = state
            recalled_message_ids.discard(message_id)
    return state


async def _unregister_active_run(state: ActiveCodexRun) -> None:
    async with active_runs_guard:
        for message_id in state.message_ids:
            if active_runs_by_message_id.get(message_id) is state:
                active_runs_by_message_id.pop(message_id, None)


async def _run_was_interrupted(state: ActiveCodexRun) -> bool:
    async with active_runs_guard:
        return state.interrupted


async def _start_codex_run(state: ActiveCodexRun) -> bool:
    """Atomically open the turn-start window unless recall already won."""
    async with active_runs_guard:
        if state.interrupted:
            return False
        state.codex_started = True
        return True

async def _get_codex_lock(chat_id: str) -> asyncio.Lock:
    async with codex_locks_guard:
        return codex_locks.setdefault(chat_id, asyncio.Lock())


@asynccontextmanager
async def _submission_lock(chat_id):
    with cache.hold(chat_id):
        lock = await _get_codex_lock(chat_id)
        while True:
            reset = reset_tasks.get(chat_id)
            if reset is not None:
                await asyncio.shield(reset)
            await lock.acquire()
            if reset_tasks.get(chat_id) is None:
                break
            lock.release()
        try:
            yield
        finally:
            lock.release()


async def _notify_terminal(state, key):
    if state.notified:
        return
    state.notified = True
    try:
        async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
            content = (CONFIG["messages"].get("cleanupTimeout", "任务收尾超时，交付结果未确认；如后续任务被阻止，请使用 /stop 重试停止。")
                       if key == "cleanupTimeout" else CONFIG["messages"][key])
            await sending_card(state.target_id or state.chat_id, content)
        if state.probe:
            state.probe.record("notification_sent", messageKey=key)
    except Exception:
        # Delivery may have succeeded despite a lost response; don't resend blindly.
        logger.exception("最终卡片投递失败或结果不确定: run_id=%s", state.run_id)
        if state.probe:
            state.probe.record("notification_uncertain", messageKey=key)


async def _watch_run(state):
    async def stop_and_notify():
        confirmed = await _interrupt_run(state)
        await _notify_terminal(state, "taskTimeout" if confirmed else "taskTimeoutUnconfirmed")

    while not state.finished.is_set():
        await asyncio.sleep(settings()["checkIntervalSeconds"])
        if state.finished.is_set():
            break
        probe = state.probe
        reason = probe.expired()
        if reason == "cleanup_timeout":
            await _cleanup_timeout(state)
            return
        if reason:
            if state.codex_started:
                status = await FerskCodex.completed_status(state.run_id)
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
            _observe_detached(state.timeout_task)
            # 停止请求发出后仍监督收尾，不能在 worker 释放前退出。
    if state.timeout_task is not None:
        await asyncio.wait({state.timeout_task}, timeout=settings()["cardRequestTimeoutSeconds"])


async def _handle_message_batch(batch: MessageBatch, generation: int) -> None:
    if len(all_runs) + len(detached_tasks) >= CONFIG["messaging"].get("maxPendingEvents", 32):
        await sending_card(batch.union_id, "当前任务繁忙，请稍后重试。")
        return
    # History may contain a completed/active run; it must not backdate this new run's deadline.
    batch = replace(batch, messages=tuple(
        message for message in batch.messages
        if message.message_id not in processed_message_ids.get(batch.chat_id, {})
        and message.message_id not in active_runs_by_message_id
    ))
    if not batch.messages:
        return
    state = ActiveCodexRun(uuid4().hex, batch.chat_id,
                           frozenset(m.message_id for m in batch.messages), target_id=batch.union_id)
    if batch.chat_id in blocked_chats:
        await _notify_terminal(state, "stopFailed")
        return
    state.probe = RunProbe(state.run_id, state.chat_id, state.message_ids,
                          received_at=min((received_at.get(mid, time.monotonic())
                                           for mid in state.message_ids), default=time.monotonic()))
    probes[state.run_id] = state.probe
    all_runs[state.run_id] = state
    state.probe.record("received")
    state.task = asyncio.create_task(_execute_message_batch(batch, generation, state))
    watcher = asyncio.create_task(_watch_run(state))
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
            stopping = asyncio.create_task(_interrupt_run(state))
            _observe_detached(stopping)
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
            _observe_detached(watcher)
        for pending in (state.stop_task, state.timeout_task):
            if pending is not None and not pending.done():
                pending.cancel()
                _observe_detached(pending)
        if state.task is not None and not state.task.done() and not state.released:
            await _cleanup_timeout(state)
        _release_run(state)


async def _execute_message_batch(batch: MessageBatch, generation: int, state) -> None:
    """Serialize input submission only; the original task owns the reply stream."""
    events = None
    transferred = False
    try:
        async with _submission_lock(batch.chat_id):
            if generation != chat_generations.get(batch.chat_id, 0):
                return
            batch = replace(batch, messages=tuple(
                message for message in batch.messages
                if message.message_id not in processed_message_ids.get(batch.chat_id, {})
                and message.message_id not in active_runs_by_message_id
            ))
            if not batch.messages:
                return
            for message in batch.messages:
                _remember_message(processed_message_ids, batch.chat_id, message.message_id)
            state = await _register_active_run(batch, state)
            state.probe.message_ids = state.message_ids
            state.probe.stage("preparing")
            if await _run_was_interrupted(state):
                return
            try:
                assembly = await assemble_codex_input(batch)
            except InputAssemblyError as exc:
                state.probe.finish("failed")
                if not await _run_was_interrupted(state):
                    await sending_card(
                        union_id=batch.union_id,
                        content=exc.user_message,
                    )
                return

            for notice in assembly.notices:
                if await _run_was_interrupted(state):
                    return
                await sending_card(union_id=batch.union_id, content=notice)

            if assembly.codex_input is None or await _run_was_interrupted(state):
                return
            logger.info("Received : user_id=%s, prompt=%s", batch.union_id, assembly.codex_input)
            owner = active_runs_by_chat.get(batch.chat_id)
            if batch.chat_id in blocked_chats:
                await _notify_terminal(state, "stopFailed")
                return
            if owner is not None:
                async with owner.controls, owner.cards.steering(CONFIG["messages"]["steerAccepted"]) as rotation:
                    if not owner.interrupted:
                        result = await FerskCodex.steer(
                            owner.run_id, assembly.codex_input,
                            cancelled=lambda: state.interrupted,
                        )
                        if result["type"] == "cancelled":
                            return
                        if result["type"] == "steered":
                            async with active_runs_guard:
                                owner.message_ids |= state.message_ids
                                for message_id in state.message_ids:
                                    active_runs_by_message_id[message_id] = owner
                                if state.interrupted:
                                    owner.interrupted = True
                            transferred = True
                            state.probe.finish("steered")
                            if owner.probe:
                                owner.probe.message_ids = owner.message_ids
                                owner.probe.record("steered", sourceRunId=state.run_id)
                            if owner.interrupted:
                                await _interrupt_run(owner)
                            else:
                                rotation.decide(True)
                                delivered = await asyncio.shield(rotation.applied)
                                if owner.probe:
                                    owner.probe.record("steer_card_rotated", delivered=delivered,
                                                       sourceRunId=state.run_id)
                            return
                        if result["type"] == "error":
                            if not await _run_was_interrupted(state):
                                await sending_card(batch.union_id, result["content"])
                            return
                # Idle means the server finished, but the original stream may
                # still be draining. Interrupted runs also wait here.
                await owner.finished.wait()

            if batch.chat_id in blocked_chats:
                await _notify_terminal(state, "stopFailed")
                return

            if generation != chat_generations.get(batch.chat_id, 0) or not await _start_codex_run(state):
                return
            state.probe.stage("starting")
            events = FerskCodex.running(
                user_id=batch.union_id, prompt=assembly.codex_input,
                run_id=state.run_id, notify_started=True,
            )
            first = await anext(events, None)
            if first is None:
                raise RuntimeError("Codex 启动后未返回任何状态")
            if first["type"] != "started":
                state.probe.finish("failed" if first["type"] == "error" else "completed")
                if not await _run_was_interrupted(state):
                    await sending_card(batch.union_id, first.get("content", ""))
                return
            state.cards = CardStreamSession(cancelled=lambda: state.interrupted)
            active_runs_by_chat[batch.chat_id] = state
            state.probe.thread_id = first.get("thread_id")
            state.probe.turn_id = first.get("turn_id")
            state.probe.stage("running")
        # No submission lock is held during model output or CardKit updates.
        async with aclosing(state.cards.events(events)) as output_events, aclosing(
            _reply_content(batch, assembly.codex_input, state, events=output_events)
        ) as content:
            try:
                await sending_card(union_id=batch.union_id, content=content, session=state.cards)
            except CardDeliveryError as exc:
                # The sender drains the model stream before reporting delivery failure.
                # A CardKit outage must not interrupt an otherwise healthy Codex turn.
                logger.error("卡片交付失败: run_id=%s, error_type=%s", state.run_id, type(exc).__name__)
                state.probe.record("delivery_failed", errorType=type(exc).__name__)
    except Exception as exc:
        logger.exception("消息批次处理异常: chat_id=%s, error=%s", batch.chat_id, exc)
        state.probe.record("exception", errorType=type(exc).__name__)
        if not state.interrupted:
            state.probe.finish("failed")
            state.interrupted = True
            state.probe.stop_reason = "failed"
            await _interrupt_run(state)
            await _notify_terminal(state, "codexFailure")
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
                    if not await FerskCodex.force_close(state.run_id) and not state.expired:
                        blocked_chats[state.chat_id] = state
                        state.probe.stop_confirmed = False
                        state.probe.record("cleanup_unconfirmed")
                finally:
                    try:
                        async with asyncio.timeout(settings()["interruptGraceSeconds"] + settings()["cleanupTimeoutSeconds"]):
                            if state.stop_task:
                                await asyncio.shield(state.stop_task)
                            async with state.controls:
                                if not transferred:
                                    await _unregister_active_run(state)
                                if active_runs_by_chat.get(batch.chat_id) is state:
                                    active_runs_by_chat.pop(batch.chat_id, None)
                                await FerskCodex.forget_run(state.run_id)
                            if not transferred:
                                await _clear_reaction(batch.chat_id, {
                                    mid for mid in state.message_ids
                                    if active_runs_by_message_id.get(mid) in (None, state)
                                })
                    except Exception:
                        logger.exception("任务收尾失败: run_id=%s", state.run_id)
                        if not transferred:
                            await _unregister_active_run(state)
                        if active_runs_by_chat.get(batch.chat_id) is state:
                            active_runs_by_chat.pop(batch.chat_id, None)
            finally:
                state.finished.set()


async def _reply_content(batch, codex_input, state, *, events=None):
    """持续展示推理和进度；最终答案替换正文，工具事件不展示。"""
    answer_started = False
    last_text_item = None
    async with aclosing(events if events is not None else FerskCodex.running(
        user_id=batch.union_id, prompt=codex_input, run_id=state.run_id,
    )) as events:
        async for event in events:
            if await _run_was_interrupted(state):
                raise CardStreamStopped()
            event_type = event.get("type")
            chunk = event.get("content", "")
            if event_type == "card_control":
                rotation = event["control"]
                yield rotation
                if rotation.accepted:
                    answer_started = False
                    last_text_item = None
            elif event_type == "reasoning" or (
                event_type == "answer" and event.get("phase") == "commentary"
            ):
                if chunk:
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
    if await _run_was_interrupted(state):
        raise CardStreamStopped()


async def _clear_reaction(
    chat_id: str,
    message_ids: frozenset[str] | set[str] | None = None,
) -> None:
    # 固定本次清理范围；网络请求期间新到达的消息留给所属批次处理。
    reactions = list(reaction_message_ids.get(chat_id, {}).items())
    for message_id, reaction_id in reactions:
        if message_ids is not None and message_id not in message_ids:
            continue
        key = (chat_id, message_id, reaction_id)
        if key in reactions_being_cleared:
            continue
        if reaction_message_ids.get(chat_id, {}).get(message_id) != reaction_id:
            continue
        reactions_being_cleared.add(key)
        try:
            succeeded = await delete_reaction_emoji(
                message_id=message_id, reaction_id=reaction_id,
            )
            if succeeded:
                current = reaction_message_ids.get(chat_id, {})
                if current.get(message_id) == reaction_id:
                    current.pop(message_id)
                    if not current:
                        reaction_message_ids.pop(chat_id, None)
        except Exception as exc:
            logger.exception("清理消息 reaction 异常: chat_id=%s, message_id=%s", chat_id, message_id)
        finally:
            reactions_being_cleared.discard(key)


async def processing_recall(data) -> None:
    """Interrupt the Codex run associated with an owner-recalled message."""
    event = data.event
    if event.recall_type != "message_owner":
        return

    message_id = event.message_id
    chat_id = event.chat_id
    async with active_runs_guard:
        state = active_runs_by_message_id.get(message_id)
        if state is None:
            state = next((run for run in all_runs.values()
                          if run.chat_id == chat_id and message_id in run.message_ids), None)
        if state is None:
            cache.recall(message_id)
        else:
            state.interrupted = True

    async with buffer_guard:
        buffered = buffered_events.get(chat_id)
        buffered_message = getattr(getattr(buffered, "event", None), "message", None)
        if getattr(buffered_message, "message_id", None) == message_id:
            buffered_events.pop(chat_id, None)
            cache._buffer_times.pop(chat_id, None)
            task = buffer_tasks.pop(chat_id, None)
            if task is not None:
                task.cancel()
            recalled_message_ids.discard(message_id)

    if state is not None:
        if state.probe and not state.probe.stop_reason:
            state.probe.stop_reason = "recalled"
        succeeded = await _interrupt_run(state)
        await _notify_terminal(state, "recallStopped" if succeeded else "stopFailed")
    try:
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            await _clear_reaction(chat_id, {message_id})
    except Exception:
        logger.exception("撤回消息 reaction 清理失败: message_id=%s", message_id)
    finally:
        cache.release_idle(chat_id)


async def _interrupt_run(state: ActiveCodexRun) -> bool:
    """One stop operation per run. A failed confirmation can be retried by /stop."""
    caller = asyncio.current_task()

    async def stop():
        confirmed = not state.codex_started
        if state.probe:
            state.probe.begin_cleanup()
            state.probe.stage("stopping")
        try:
            if state.codex_started:
                confirmed = await FerskCodex.interrupt_and_confirm(state.run_id)
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
            if blocked_chats.get(state.chat_id) is state:
                blocked_chats.pop(state.chat_id, None)
                if state.finished.is_set():
                    probes.pop(state.run_id, None)
        elif not state.expired:
            blocked_chats[state.chat_id] = state
        if state.task is not None and state.task is not caller and not state.task.done():
            state.task.cancel()
        return confirmed

    if state.stop_task is None:
        state.stop_task = asyncio.create_task(stop())
    return await asyncio.shield(state.stop_task)


async def _stop_chat(data, *, advance_generation=True):
    """停止本会话当前任务和已接收的待处理输入，保留 thread 绑定。"""
    message = data.event.message
    chat_id = message.chat_id
    reaction_ids = set(reaction_message_ids.get(chat_id, {}))
    async with active_runs_guard:
        if advance_generation:
            chat_generations[chat_id] = chat_generations.get(chat_id, 0) + 1
        states = {
            state.run_id: state for state in active_runs_by_message_id.values()
            if state.chat_id == chat_id
        }
        states.update({state.run_id: state for state in all_runs.values()
                       if state.chat_id == chat_id})
        blocked = blocked_chats.get(chat_id)
        if blocked:
            blocked.stop_task = None
            blocked.notified = False
            states[blocked.run_id] = blocked
        for state in states.values():
            state.interrupted = True
            if state.probe and not state.probe.stop_reason:
                state.probe.stop_reason = "stopped"
        had_work = bool(states or pending_chat_requests.get(chat_id) or chat_id in buffered_events)
    await _cancel_buffer(chat_id)
    results = await asyncio.gather(*(_interrupt_run(state) for state in states.values()))
    succeeded = all(results)
    try:
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            await _clear_reaction(chat_id, reaction_ids)
    except Exception:
        logger.exception("停止后 reaction 清理失败: chat_id=%s", chat_id)
    return states, succeeded, had_work


async def processing_stop(data) -> None:
    states, succeeded, had_work = await _stop_chat(data)
    message = data.event.message
    chat_id = message.chat_id
    key = "stopFailed" if not succeeded else "stopRequested" if had_work else "stopIdle"
    target_id = chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
    if states and all(state.notified for state in states.values()):
        return
    for state in states.values():
        state.notified = True
    try:
        async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
            await sending_card(union_id=target_id, content=CONFIG["messages"][key])
        for state in states.values():
            if state.probe:
                state.probe.record("notification_sent", messageKey=key)
    except Exception:
        logger.exception("停止卡片投递失败或结果不确定: chat_id=%s", chat_id)


async def history_options(data) -> list[dict]:
    """前端适配入口：data 必须来自已验证的事件，不能由客户端伪造身份。"""
    message = data.event.message
    target_id = message.chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
    return [
        {"label": record.thread_name or "未命名会话", "value": record.thread_id,
         "updated_at": record.updated_at}
        for record in await list_sessions(target_id)
    ]


async def processing_history(data):
    """仅私聊展示个人历史；不停止任务、不修改活跃绑定。"""
    if data.event.message.chat_type != "p2p":
        return
    user_id = data.event.sender.sender_id.union_id
    if not user_id:
        logger.error("历史卡片缺少可信 union_id")
        return
    card = None
    try:
        options = await history_options(data)
        card = history_cards.create(user_id, data.event.message.chat_id, options)
        card.message_id = await interactive.send_interactive_card(user_id, interactive.build_history_card(card))
    except Exception:
        if card is not None:
            history_cards.cards.pop(card.token, None)
        logger.exception("历史卡片加载或发送失败")
        await interactive.send_interactive_card(user_id, interactive.status_card("历史会话加载或发送失败，请重新发送 /history。"))


async def processing_history_action(data):
    """主事件循环内校验、去重并串行恢复；SDK 回调不等待恢复完成。"""
    try:
        card = history_cards.resolve(data)
    except (ValueError, AttributeError):
        # 未校验的回调不得用于向任意用户发送消息。
        logger.warning("历史卡片回调被拒绝：上下文失效、不匹配或重复操作")
        return
    value = data.event.action.value
    try:
        if value.get("action") == "history_page":
            page = value.get("page")
            if (data.event.action.tag != "button" or type(page) is not int
                    or abs(page - card.page) != 1
                    or not 0 <= page < (len(card.options) + interactive.PAGE_SIZE - 1) // interactive.PAGE_SIZE):
                raise ValueError("无效的历史页码")
            card.busy = True
            updated = replace(card, page=page, revision=card.revision + 1, busy=False)
            await interactive.update_interactive_card(card.message_id, interactive.build_history_card(updated))
            history_cards.cards[card.token] = updated
            return
        thread_id = interactive.confirmed_thread_id(data, card)
    except ValueError as exc:
        await interactive.send_interactive_card(card.user_id, interactive.status_card(str(exc)))
        return
    except Exception:
        logger.exception("历史卡片翻页失败")
        card.busy = False
        await interactive.send_interactive_card(card.user_id, interactive.status_card("翻页失败，请重新发送 /history。"))
        return
    card.busy = True
    # 每张卡片的确认仅消费一次；失败后重新 /history，避免重投回调再次停止任务。
    card.finished = True
    try:
        with cache.hold(card.chat_id):
            result = await processing_history_restore(card.message_event(), thread_id)
        label = next(item["label"] for item in card.visible_options if item["value"] == thread_id)
        text = f"已激活：{label}" if result["ok"] else "激活失败，未切换当前线程绑定。请重新发送 /history 后重试。"
        body = interactive.status_card(text)
        try:
            await interactive.update_interactive_card(card.message_id, body)
        except Exception:
            logger.exception("激活结果卡片更新失败；不重试恢复操作")
            await interactive.send_interactive_card(card.user_id, body)
    finally:
        card.busy = False


def dispatch_history_action(dispatcher, data):
    """同步 SDK 入口，只提交已知动作；不把入队成功报告为线程激活成功。"""
    action = getattr(getattr(data, "event", None), "action", None)
    value = getattr(action, "value", None)
    if not isinstance(value, dict) or value.get("action") not in {"activate_history", "history_page"}:
        return interactive.callback_response("未执行任何操作")
    try:
        # 这里只读检查，以便过期/转发卡片得到即时提示；主循环在执行前再次校验。
        history_cards.resolve(data)
    except ValueError as exc:
        return interactive.callback_response(str(exc), error=True)
    except AttributeError:
        return interactive.callback_response("卡片回调缺少必要身份或上下文", error=True)
    if not dispatcher.submit(processing_history_action, data, control=True):
        return interactive.callback_response("当前任务繁忙，请稍后重试", error=True)
    return interactive.callback_response("正在处理，请以卡片最终结果为准")


async def processing_history_restore(data, thread_id: str) -> dict:
    """返回供前端展示的结果；复用 /new 的停止、等待及提交隔离流程。"""
    chat_id = data.event.message.chat_id
    target_id = chat_id if data.event.message.chat_type == "group" else data.event.sender.sender_id.union_id
    previous = reset_tasks.get(chat_id)

    async def restore():
        try:
            if previous is not None:
                await asyncio.shield(previous)
            async with asyncio.timeout(settings()["startupTimeoutSeconds"]):
                if not isinstance(thread_id, str) or not thread_id or await get_session(target_id, thread_id) is None:
                    raise ValueError("历史会话不存在或不属于当前用户")
                if await get_user_thread(target_id) == thread_id:
                    return {"ok": True, "thread_id": thread_id, "content": "已恢复历史会话"}
                chat_generations[chat_id] = chat_generations.get(chat_id, 0) + 1
                states, succeeded, _ = await _stop_chat(data, advance_generation=False)
                for state in states.values():
                    state.notified = True
                if not succeeded:
                    raise RuntimeError("当前任务停止未确认")
                for state in states.values():
                    if state.task is not None:
                        await state.finished.wait()
                async with await _get_codex_lock(chat_id):
                    await FerskCodex.restore_session(target_id, thread_id)
            return {"ok": True, "thread_id": thread_id, "content": "已恢复历史会话"}
        except Exception:
            logger.exception("恢复历史会话失败: chat_id=%s", chat_id)
            return {"ok": False, "content": "恢复历史会话失败"}
        finally:
            if reset_tasks.get(chat_id) is asyncio.current_task():
                reset_tasks.pop(chat_id, None)
                cache.release_idle(chat_id)

    # 第一次 await 之前建立门禁，后续普通输入会等待本次切换完成。
    task = asyncio.create_task(restore())
    reset_tasks[chat_id] = task
    return await asyncio.shield(task)


async def processing_new(data) -> None:
    """Gate subsequent submissions before the first await; stop then reset."""
    chat_id = data.event.message.chat_id
    target_id = chat_id if data.event.message.chat_type == "group" else data.event.sender.sender_id.union_id
    previous = reset_tasks.get(chat_id)
    chat_generations[chat_id] = chat_generations.get(chat_id, 0) + 1

    async def reset():
        try:
            if previous is not None:
                await asyncio.shield(previous)
            states, succeeded, _ = await _stop_chat(data, advance_generation=False)
            for state in states.values():
                state.notified = True
            key = "stopFailed"
            if succeeded:
                try:
                    async with asyncio.timeout(settings()["startupTimeoutSeconds"]):
                        # A cancelled input worker must release its submission lock
                        # and finish cleanup before its binding can be replaced.
                        for state in states.values():
                            if state.task is not None:
                                await state.finished.wait()
                        async with await _get_codex_lock(chat_id):
                            await FerskCodex.reset_thread(target_id)
                    key = "newThreadCreated"
                except Exception:
                    logger.exception("新会话重置失败: chat_id=%s", chat_id)
                    key = "newThreadFailed"
            async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                await sending_card(union_id=target_id, content=CONFIG["messages"][key])
        except Exception:
            logger.exception("新会话命令处理或通知失败: chat_id=%s", chat_id)
        finally:
            if reset_tasks.get(chat_id) is asyncio.current_task():
                reset_tasks.pop(chat_id, None)
                cache.release_idle(chat_id)

    task = asyncio.create_task(reset())
    reset_tasks[chat_id] = task
    await asyncio.shield(task)


router = MessageRouter(
    cache, submit=lambda *args: _handle_message_batch(*args),
    stop=lambda data: processing_stop(data), new=lambda data: processing_new(data),
    notify=lambda *args: _notify_terminal(*args),
    fetch_history=lambda **kwargs: getting_chat_history(**kwargs),
    add_reaction=lambda **kwargs: adding_reaction_emoji(**kwargs),
    clear_reactions=lambda *args: _clear_reaction(*args),
    send_card=lambda *args: sending_card(*args),
    buffer_seconds=lambda: message_buffer_seconds,
    history=lambda data: processing_history(data),
)
processing = router.processing
_cancel_buffer = router._cancel_buffer
_buffer_message = router._buffer_message
_process_chat_history = router._process_chat_history
_route_message = router._route_message


async def _expire_session_cache():
    history_cards.prune()
    for state in cache.prune():
        state.expired = True
        state.interrupted = True
        task = state.task
        if task is not None and not task.done():
            task.cancel()
        try:
            await FerskCodex.discard_expired_run(state.run_id)
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
                cache.expire_run(state)
                probes.pop(state.run_id, None)
                logger.warning("已清理超过 24 小时的任务缓存: run_id=%s", state.run_id)


async def _maintain_session_cache():
    while True:
        await asyncio.sleep(settings()["checkIntervalSeconds"])
        try:
            await _expire_session_cache()
        except Exception:
            logger.exception("会话缓存清理失败")


async def main() -> None:

    configure_logging(CONFIG["logging"].get("logLevel", "INFO"))
    _bot_identity(required=True)
    loop = asyncio.get_running_loop()
    dispatcher = EventDispatcher(loop, CONFIG["messaging"].get("maxPendingEvents", 32))
    notices = EventDispatcher(loop, capacity=1)

    async def busy(data):
        await sending_card(data.event.message.chat_id, "当前任务繁忙，请稍后重试。")

    def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        message = data.event.message
        logger.debug("收到消息: chat_id=%s, message_id=%s, type=%s",
                     message.chat_id, message.message_id, message.message_type)
        control = (is_stop_command(message.message_type, message.content)
                   or is_new_command(message.message_type, message.content)
                   or (message.chat_type == "p2p" and is_history_command(message.message_type, message.content)))
        if not dispatcher.submit(processing, data, control=control):
            notices.submit(busy, data)

    def do_p2_im_message_recalled_v1(data: lark.im.v1.P2ImMessageRecalledV1) -> None:
        dispatcher.submit(processing_recall, data, control=True)

    def do_p2_im_message_read_v1(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
        pass

    def do_p2_im_message_reaction_created_v1(data: lark.im.v1.P2ImMessageReactionCreatedV1) -> None:
        pass

    def do_p2_im_message_reaction_deleted_v1(data: lark.im.v1.P2ImMessageReactionDeletedV1) -> None:
        pass

    def do_p2_im_chat_access_event_bot_p2p_chat_entered_v1(data: lark.im.v1.P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        pass

    event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
            .register_p2_card_action_trigger(lambda data: dispatch_history_action(dispatcher, data))
            .register_p2_im_message_recalled_v1(do_p2_im_message_recalled_v1)
            .register_p2_im_message_message_read_v1(do_p2_im_message_read_v1)
            .register_p2_im_message_reaction_created_v1(do_p2_im_message_reaction_created_v1)
            .register_p2_im_message_reaction_deleted_v1(do_p2_im_message_reaction_deleted_v1)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(do_p2_im_chat_access_event_bot_p2p_chat_entered_v1)
            .build()
        )
    
    websocket_client = create_websocket_client(event_handler)
    maintenance = asyncio.create_task(_maintain_session_cache())
    try:
        await asyncio.to_thread(websocket_client.start)
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        for state in list(all_runs.values()):
            state.interrupted = True
            state.probe.stop_reason = state.probe.stop_reason or "shutdown"
        await asyncio.gather(*(_interrupt_run(state) for state in list(all_runs.values())),
                             return_exceptions=True)
        try:
            await journal.flush()
        except Exception:
            logger.exception("退出时运行日志尚未写入完成")


def cli() -> None:
    """同步命令入口，负责启动异步网关。"""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
