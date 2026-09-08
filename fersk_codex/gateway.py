import asyncio
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field, replace
from contextlib import aclosing, asynccontextmanager
from uuid import uuid4

import lark_oapi as lark

# Direct script execution needs the package's parent on the import path.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fersk_codex.core.codex import FerskCodex
from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger
from fersk_codex.core.thread_watchdog import RunProbe, probes, settings, journal
from fersk_codex.services.lark.lark_client import create_websocket_client
from fersk_codex.services.lark.lark_tools import getting_chat_history, adding_reaction_emoji, delete_reaction_emoji
from fersk_codex.services.lark.lark_card import CardDeliveryError, CardReplace, CardStreamSession, CardStreamStopped, sending_card
from fersk_codex.middleware.message_collector import MessageBatch, batch_from_chat_history, is_stop_command, is_new_command
from fersk_codex.middleware.message_assemble import InputAssemblyError, assemble_codex_input

message_buffer_seconds = CONFIG["messaging"]["bufferWindowSeconds"]
history_messages_num = CONFIG["messaging"]["historyPageSize"]

codex_locks: dict[str, asyncio.Lock] = {}
codex_locks_guard = asyncio.Lock()
reset_tasks: dict[str, asyncio.Task] = {}
direct_message_types = set(CONFIG["messaging"]["directTypes"])
buffered_message_types = set(CONFIG["messaging"]["bufferedTypes"])
buffered_events: dict[str, object] = {}
buffer_tasks: dict[str, asyncio.Task[None]] = {}
buffer_guard = asyncio.Lock()
reaction_message_ids: dict[str, dict[str, str]] = {}
reactions_being_cleared: set[tuple[str, str, str]] = set()
recalled_message_ids: set[str] = set()
chat_generations: dict[str, int] = {}
pending_chat_requests: dict[str, int] = {}
received_message_ids: dict[str, dict[str, None]] = {}
processed_message_ids: dict[str, dict[str, None]] = {}
received_at: dict[str, float] = {}

logger = get_logger("Message")


@dataclass
class ActiveCodexRun:
    run_id: str
    chat_id: str
    message_ids: frozenset[str]
    interrupted: bool = False
    codex_started: bool = False
    controls: asyncio.Lock = field(default_factory=asyncio.Lock)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    target_id: str = ""
    probe: RunProbe | None = None
    task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None
    notified: bool = False
    cards: CardStreamSession | None = None


active_runs_by_message_id: dict[str, ActiveCodexRun] = {}
active_runs_guard = asyncio.Lock()
active_runs_by_chat: dict[str, ActiveCodexRun] = {}
all_runs: dict[str, ActiveCodexRun] = {}
blocked_chats: dict[str, ActiveCodexRun] = {}


def _remember_message(journal, chat_id, message_id):
    entries = journal.setdefault(chat_id, {})
    entries[message_id] = None
    while len(entries) > CONFIG["messaging"]["recallCacheMaxEntries"]:
        entries.pop(next(iter(entries)))


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
            await sending_card(state.target_id or state.chat_id, CONFIG["messages"][key])
        if state.probe:
            state.probe.record("notification_sent", messageKey=key)
    except Exception:
        # Delivery may have succeeded despite a lost response; don't resend blindly.
        logger.exception("最终卡片投递失败或结果不确定: run_id=%s", state.run_id)
        if state.probe:
            state.probe.record("notification_uncertain", messageKey=key)


async def _watch_run(state):
    while not state.finished.is_set():
        await asyncio.sleep(settings()["checkIntervalSeconds"])
        probe = state.probe
        if probe.terminal or probe.stop_reason:
            continue
        reason = probe.expired()
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
            confirmed = await _interrupt_run(state)
            await _notify_terminal(state, "taskTimeout" if confirmed else "taskTimeoutUnconfirmed")
            return


async def _handle_message_batch(batch: MessageBatch, generation: int) -> None:
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
        await state.task
    except asyncio.CancelledError:
        if not state.interrupted:
            state.interrupted = True
            state.probe.stop_reason = "shutdown"
            await _interrupt_run(state)
            raise
    finally:
        if state.stop_task:
            await asyncio.shield(state.stop_task)
        if state.probe.stop_reason and watcher is not asyncio.current_task():
            # A watchdog owns its notification even if cancelling the worker finishes first.
            if not watcher.done() and state.probe.stop_reason.endswith("timeout"):
                await watcher
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        state.finished.set()
        state.probe.finish(state.probe.stop_reason or "completed")
        state.probe.record("released")
        all_runs.pop(state.run_id, None)
        if blocked_chats.get(state.chat_id) is not state:
            probes.pop(state.run_id, None)


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
                logger.exception("卡片交付失败: run_id=%s", state.run_id)
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
                    if not await FerskCodex.force_close(state.run_id):
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
            print(
                f"清理消息 reaction 异常: chat_id={chat_id}, "
                f"message_id={message_id}, error={exc}"
            )
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
            recalled_message_ids.add(message_id)
            # Keep a bounded race buffer for recalls arriving just before
            # receive processing; completed or unrelated recalls need no
            # permanent history in this process.
            while len(recalled_message_ids) > CONFIG["messaging"]["recallCacheMaxEntries"]:
                recalled_message_ids.pop()
        else:
            state.interrupted = True

    async with buffer_guard:
        buffered = buffered_events.get(chat_id)
        buffered_message = getattr(getattr(buffered, "event", None), "message", None)
        if getattr(buffered_message, "message_id", None) == message_id:
            buffered_events.pop(chat_id, None)
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


async def _interrupt_run(state: ActiveCodexRun) -> bool:
    """One stop operation per run. A failed confirmation can be retried by /stop."""
    caller = asyncio.current_task()

    async def stop():
        confirmed = not state.codex_started
        if state.probe:
            state.probe.stage("stopping")
        try:
            if state.codex_started:
                confirmed = await FerskCodex.interrupt_and_confirm(state.run_id)
        except Exception:
            logger.exception("停止任务失败: run_id=%s", state.run_id)
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
        else:
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
    succeeded = True
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

    task = asyncio.create_task(reset())
    reset_tasks[chat_id] = task
    await asyncio.shield(task)


async def processing(data) -> None:
    """Route direct messages immediately and buffer image/file messages."""
    try:
        chat_type = data.event.message.chat_type
        if chat_type == "group" and not _is_bot_mentioned(data.event.message.mentions):
            return
        if chat_type not in {"p2p", "group"}:
            return
        message = data.event.message
        if message.message_id in received_message_ids.get(message.chat_id, {}):
            return
        _remember_message(received_message_ids, message.chat_id, message.message_id)
        received_at[message.message_id] = time.monotonic()
        while len(received_at) > CONFIG["messaging"]["recallCacheMaxEntries"]:
            received_at.pop(next(iter(received_at)))
        if is_stop_command(message.message_type, message.content):
            await processing_stop(data)
            return
        if is_new_command(message.message_type, message.content):
            await processing_new(data)
            return
        chat_id = message.chat_id
        generation = chat_generations.get(chat_id, 0)
        while (reset := reset_tasks.get(chat_id)) is not None:
            await asyncio.shield(reset)
        if generation != chat_generations.get(chat_id, 0):
            return
        pending_chat_requests[chat_id] = pending_chat_requests.get(chat_id, 0) + 1
        try:
            try:
                async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                    reaction_id = await adding_reaction_emoji(message_id=message.message_id)
            except Exception:
                logger.exception("添加 reaction 失败，继续处理消息: message_id=%s", message.message_id)
                reaction_id = None
            if reaction_id is not None:
                reaction_message_ids.setdefault(chat_id, {})[message.message_id] = reaction_id
            if generation != chat_generations.get(chat_id, 0):
                if reaction_id is not None:
                    await _clear_reaction(chat_id, {message.message_id})
                return
            if message.message_id in processed_message_ids.get(chat_id, {}):
                # History may have included this attachment before its receive
                # event completed the reaction request.
                if message.message_id not in active_runs_by_message_id:
                    await _clear_reaction(chat_id, {message.message_id})
                return
            await _route_message(data, generation)
        finally:
            pending_chat_requests[chat_id] -= 1
            if not pending_chat_requests[chat_id]:
                pending_chat_requests.pop(chat_id)
    except Exception as exc:
        print(f"解析消息异常: {exc}")


async def _route_message(data, generation: int) -> None:
    """Apply the configured direct/buffered behavior to one message."""
    message_type = data.event.message.message_type
    if message_type in buffered_message_types:
        await _buffer_message(data, generation)
        return

    if message_type in direct_message_types:
        await _cancel_buffer(data.event.message.chat_id)
        await _process_chat_history(data, generation)
        return
    message = data.event.message
    target_id = message.chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
    try:
        await sending_card(target_id, CONFIG["messages"]["unsupportedMessageType"])
    finally:
        await _clear_reaction(message.chat_id, {message.message_id})


async def _buffer_message(data, generation: int) -> None:
    """Start one fixed window per chat without extending it on new messages."""
    chat_id = data.event.message.chat_id
    async with buffer_guard:
        if generation != chat_generations.get(chat_id, 0):
            return
        # Keep the latest event so the history fallback is anchored to the
        # newest message received during this fixed window.
        buffered_events[chat_id] = data
        task = buffer_tasks.get(chat_id)
        if task is None or task.done():
            buffer_tasks[chat_id] = asyncio.create_task(
                _flush_after_fixed_window(chat_id, generation)
            )


async def _cancel_buffer(chat_id: str) -> None:
    """Cancel a pending attachment window taken over by a direct message."""
    async with buffer_guard:
        buffered_events.pop(chat_id, None)
        task = buffer_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()


def _bot_identity(*, required=False):
    """Resolve optional identity settings; reject an unusable startup config."""
    credentials = CONFIG["lark"]["credentials"]
    def value(key):
        env_name = credentials.get(key)
        return (os.getenv(env_name, "").strip() if env_name else "")
    robot_open_id = value("robotOpenIdEnv")
    robot_name = value("robotNameEnv")
    if required and not (robot_open_id or robot_name):
        raise RuntimeError("群聊机器人标识未配置：robotOpenIdEnv 或 robotNameEnv 对应的环境变量至少一个非空")
    return robot_open_id, robot_name


def _is_bot_mentioned(mentions) -> bool:
    """Match a nonempty Open ID or name; Open ID survives bot renaming."""
    robot_open_id, robot_name = _bot_identity()
    for mention in mentions or []:
        mention_id = getattr(mention, "id", None)
        mentioned_open_id = getattr(mention_id, "open_id", None)
        if robot_open_id and mentioned_open_id == robot_open_id:
            return True
        if robot_name and getattr(mention, "name", None) == robot_name:
            return True
    return False


async def _flush_after_fixed_window(chat_id: str, generation: int) -> None:
    """Fetch and process history exactly once after the original 10 seconds."""
    try:
        await asyncio.sleep(message_buffer_seconds)
        async with buffer_guard:
            data = buffered_events.pop(chat_id, None)
            buffer_tasks.pop(chat_id, None)

        if data is None:
            return
        await _process_chat_history(data, generation)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"消息缓冲处理异常: chat_id={chat_id}, error={exc}")


async def _process_chat_history(data, generation: int) -> None:
    """Fetch the latest history and submit the current user turn to Codex."""

    current_message_id = data.event.message.message_id
    if generation != chat_generations.get(data.event.message.chat_id, 0):
        return
    async with active_runs_guard:
        if current_message_id in recalled_message_ids:
            recalled_message_ids.discard(current_message_id)
            was_recalled = True
        else:
            was_recalled = False
    if was_recalled:
        await _clear_reaction(data.event.message.chat_id, {current_message_id})
        return

    try:
        remaining = settings()["maxRunSeconds"] - (time.monotonic() - received_at.get(
            current_message_id, time.monotonic()))
        async with asyncio.timeout(max(0, remaining)):
            history_items = await getting_chat_history(
                chat_id=data.event.message.chat_id,
                messages_num=history_messages_num,
            )
    except Exception:
        logger.exception("获取历史消息失败: message_id=%s", current_message_id)
        state = ActiveCodexRun(uuid4().hex, data.event.message.chat_id,
                               frozenset({current_message_id}),
                               target_id=(data.event.message.chat_id if data.event.message.chat_type == "group"
                                          else data.event.sender.sender_id.union_id))
        state.probe = RunProbe(state.run_id, state.chat_id, state.message_ids)
        state.probe.finish("failed")
        if generation == chat_generations.get(state.chat_id, 0):
            await _notify_terminal(state, "codexFailure")
        await _clear_reaction(state.chat_id, state.message_ids)
        return
    batch = batch_from_chat_history(data, history_items)
    if batch.messages:
        await _handle_message_batch(batch, generation)


async def main() -> None:

    _bot_identity(required=True)
    loop = asyncio.get_running_loop()

    def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        print(f'[ do_p2_im_message_receive_v1 access ], data: {lark.JSON.marshal(data, indent=4)}')
        asyncio.run_coroutine_threadsafe(processing(data), loop)

    def do_p2_im_message_recalled_v1(data: lark.im.v1.P2ImMessageRecalledV1) -> None:
        print(f'[ do_p2_im_message_recalled_v1 access ], data: {lark.JSON.marshal(data, indent=4)}')
        asyncio.run_coroutine_threadsafe(processing_recall(data), loop)

    def do_p2_im_message_read_v1(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
        pass

    def do_p2_im_message_reaction_created_v1(data: lark.im.v1.P2ImMessageReactionCreatedV1) -> None:
        pass

    def do_p2_im_message_reaction_deleted_v1(data: lark.im.v1.P2ImMessageReactionDeletedV1) -> None:
        pass

    event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
            .register_p2_im_message_recalled_v1(do_p2_im_message_recalled_v1)
            .register_p2_im_message_message_read_v1(do_p2_im_message_read_v1)
            .register_p2_im_message_reaction_created_v1(do_p2_im_message_reaction_created_v1)
            .register_p2_im_message_reaction_deleted_v1(do_p2_im_message_reaction_deleted_v1)
            .build()
        )
    
    websocket_client = create_websocket_client(event_handler)
    try:
        await asyncio.to_thread(websocket_client.start)
    finally:
        for state in list(all_runs.values()):
            state.interrupted = True
            state.probe.stop_reason = state.probe.stop_reason or "shutdown"
        await asyncio.gather(*(_interrupt_run(state) for state in list(all_runs.values())),
                             return_exceptions=True)
        try:
            await journal.flush()
        except Exception:
            logger.exception("退出时运行日志尚未写入完成")


if __name__ == "__main__":
    asyncio.run(main())
