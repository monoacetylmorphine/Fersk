"""飞书消息入口、固定窗口缓冲与历史收集，通过回调提交任务。"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger
from fersk_codex.codex.thread_watchdog import RunProbe, settings
from fersk_codex.session.session_gateway import ActiveCodexRun, RETENTION_SECONDS
from fersk_codex.middleware.message_collector import batch_from_chat_history, is_new_command, is_stop_command, is_history_command

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import Message, MentionEvent, P2ImMessageReceiveV1

    from fersk_codex.middleware.message_collector import MessageBatch
    from fersk_codex.session.session_gateway import SessionCache

logger = get_logger("Message")

def _bot_identity(*, required: bool = False) -> tuple[str, str]:
    """Resolve optional identity settings; reject an unusable startup config."""
    credentials = CONFIG["lark"]["credentials"]
    def value(key: str) -> str:
        env_name = credentials.get(key)
        return (os.getenv(env_name, "").strip() if env_name else "")
    robot_union_id = value("robotUnionIdEnv")
    robot_name = value("robotNameEnv")
    if required and not (robot_union_id or robot_name):
        raise RuntimeError("群聊机器人标识未配置：robotUnionIdEnv 或 robotNameEnv 对应的环境变量至少一个非空")
    return robot_union_id, robot_name

def _is_bot_mentioned(mentions: Iterable[MentionEvent] | None) -> bool:
    """Match a nonempty Union ID or name; Union ID survives bot renaming."""
    robot_union_id, robot_name = _bot_identity()
    for mention in mentions or []:
        mention_id = getattr(mention, "id", None)
        mentioned_union_id = getattr(mention_id, "union_id", None)
        if robot_union_id and mentioned_union_id == robot_union_id:
            return True
        if robot_name and getattr(mention, "name", None) == robot_name:
            return True
    return False

class MessageRouter:
    def __init__(
        self,
        cache: SessionCache,
        *,
        submit: Callable[[MessageBatch, int], Awaitable[None]],
        stop: Callable[[P2ImMessageReceiveV1], Awaitable[None]],
        new: Callable[[P2ImMessageReceiveV1], Awaitable[None]],
        notify: Callable[[ActiveCodexRun, str], Awaitable[None]],
        fetch_history: Callable[..., Awaitable[list[Message]]],
        add_reaction: Callable[..., Awaitable[str | None]],
        clear_reactions: Callable[..., Awaitable[None]],
        send_card: Callable[..., Awaitable[str | None]],
        buffer_seconds: Callable[[], float],
        history: Callable[[P2ImMessageReceiveV1], Awaitable[None]] | None = None,
    ) -> None:
        self.cache = cache
        self.submit = submit
        self.stop = stop
        self.new = new
        self.notify = notify
        self.fetch_history = fetch_history
        self.add_reaction = add_reaction
        self.clear_reactions = clear_reactions
        self.send_card = send_card
        self.buffer_seconds = buffer_seconds
        self.history = history

    async def processing(self, data: P2ImMessageReceiveV1) -> None:
        """登记整个入站请求，避免旧任务回收新请求正在使用的会话锁。"""
        async with asyncio.timeout(RETENTION_SECONDS):
            await self._processing(data)

    async def _processing(self, data: P2ImMessageReceiveV1) -> None:
        with self.cache.hold(data.event.message.chat_id):
            self.cache.prune()
            try:
                chat_type = data.event.message.chat_type
                if chat_type == "group" and not _is_bot_mentioned(data.event.message.mentions):
                    return
                if chat_type not in {"p2p", "group"}:
                    return
                message = data.event.message
                if message.message_id in self.cache.received_message_ids.get(message.chat_id, {}):
                    return
                self.cache.remember(self.cache.received_message_ids, message.chat_id, message.message_id)
                self.cache.received_at[message.message_id] = time.monotonic()
                while len(self.cache.received_at) > CONFIG["messaging"]["recallCacheMaxEntries"]:
                    self.cache.received_at.pop(next(iter(self.cache.received_at)))
                if (chat_type == "p2p" and self.history is not None
                        and is_history_command(message.message_type, message.content)):
                    await self.history(data)
                    return
                if is_stop_command(message.message_type, message.content):
                    await self.stop(data)
                    return
                if is_new_command(message.message_type, message.content):
                    await self.new(data)
                    return
                chat_id = message.chat_id
                generation = self.cache.chat_generations.get(chat_id, 0)
                while (reset := self.cache.reset_tasks.get(chat_id)) is not None:
                    await asyncio.shield(reset)
                if generation != self.cache.chat_generations.get(chat_id, 0):
                    return
                self.cache.pending_chat_requests[chat_id] = self.cache.pending_chat_requests.get(chat_id, 0) + 1
                try:
                    try:
                        async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                            reaction_id = await self.add_reaction(message_id=message.message_id)
                    except Exception:
                        logger.exception("添加 reaction 失败，继续处理消息: message_id=%s", message.message_id)
                        reaction_id = None
                    if reaction_id is not None:
                        self.cache.reaction_message_ids.setdefault(chat_id, {})[message.message_id] = reaction_id
                        self.cache.track_reaction(chat_id, message.message_id)
                    if generation != self.cache.chat_generations.get(chat_id, 0):
                        if reaction_id is not None:
                            await self.clear_reactions(chat_id, {message.message_id})
                        return
                    if message.message_id in self.cache.processed_message_ids.get(chat_id, {}):
                        # History may have included this attachment before its receive
                        # event completed the reaction request.
                        if message.message_id not in self.cache.active_runs_by_message_id:
                            await self.clear_reactions(chat_id, {message.message_id})
                        return
                    await self._route_message(data, generation)
                finally:
                    self.cache.pending_chat_requests[chat_id] -= 1
                    if not self.cache.pending_chat_requests[chat_id]:
                        self.cache.pending_chat_requests.pop(chat_id)
            except Exception as exc:
                logger.exception("解析消息异常: chat_id=%s", data.event.message.chat_id)
            finally:
                message = data.event.message
                buffered = self.cache.buffered_events.get(message.chat_id)
                pending = getattr(getattr(getattr(buffered, "event", None), "message", None), "message_id", None)
                if pending != message.message_id:
                    self.cache.release_messages(message.chat_id, {message.message_id})

    async def _route_message(self, data: P2ImMessageReceiveV1, generation: int) -> None:
        """Apply the configured direct/buffered behavior to one message."""
        message_type = data.event.message.message_type
        if message_type in CONFIG["messaging"]["bufferedTypes"]:
            await self._buffer_message(data, generation)
            return

        if message_type in CONFIG["messaging"]["directTypes"]:
            await self._cancel_buffer(data.event.message.chat_id)
            await self._process_chat_history(data, generation)
            return
        message = data.event.message
        target_id = message.chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
        try:
            await self.send_card(target_id, CONFIG["messages"]["unsupportedMessageType"])
        finally:
            await self.clear_reactions(message.chat_id, {message.message_id})

    async def _buffer_message(self, data: P2ImMessageReceiveV1, generation: int) -> None:
        """Start one fixed window per chat without extending it on new messages."""
        chat_id = data.event.message.chat_id
        async with self.cache.buffer_guard:
            if generation != self.cache.chat_generations.get(chat_id, 0):
                return
            # Keep the latest event so the history fallback is anchored to the
            # newest message received during this fixed window.
            self.cache.buffered_events[chat_id] = data
            self.cache._buffer_times.setdefault(chat_id, time.monotonic())
            task = self.cache.buffer_tasks.get(chat_id)
            if task is None or task.done():
                self.cache.buffer_tasks[chat_id] = asyncio.create_task(
                    self._flush_after_fixed_window(chat_id, generation)
                )

    async def _cancel_buffer(self, chat_id: str) -> None:
        """Cancel a pending attachment window taken over by a direct message."""
        async with self.cache.buffer_guard:
            self.cache.buffered_events.pop(chat_id, None)
            self.cache._buffer_times.pop(chat_id, None)
            task = self.cache.buffer_tasks.pop(chat_id, None)
            if task is not None and not task.done():
                task.cancel()

    async def _flush_after_fixed_window(self, chat_id: str, generation: int) -> None:
        """Fetch and process history exactly once after the original 10 seconds."""
        data = None
        try:
            await asyncio.sleep(min(self.buffer_seconds(), RETENTION_SECONDS))
            async with self.cache.buffer_guard:
                data = self.cache.buffered_events.pop(chat_id, None)
                self.cache._buffer_times.pop(chat_id, None)
                self.cache.buffer_tasks.pop(chat_id, None)

            if data is None:
                return
            await self._process_chat_history(data, generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("消息缓冲处理异常: chat_id=%s", chat_id)
        finally:
            if data is not None:
                self.cache.release_messages(chat_id, {data.event.message.message_id})
            self.cache.release_idle(chat_id)

    async def _process_chat_history(self, data: P2ImMessageReceiveV1, generation: int) -> None:
        """Fetch the latest history and submit the current user turn to Codex."""
        with self.cache.hold(data.event.message.chat_id):

            current_message_id = data.event.message.message_id
            if generation != self.cache.chat_generations.get(data.event.message.chat_id, 0):
                return
            async with self.cache.active_runs_guard:
                if current_message_id in self.cache.recalled_message_ids:
                    self.cache.recalled_message_ids.discard(current_message_id)
                    was_recalled = True
                else:
                    was_recalled = False
            if was_recalled:
                await self.clear_reactions(data.event.message.chat_id, {current_message_id})
                return

            try:
                remaining = min(settings()["maxRunSeconds"], RETENTION_SECONDS) - (time.monotonic() - self.cache.received_at.get(
                    current_message_id, time.monotonic()))
                async with asyncio.timeout(max(0, remaining)):
                    history_items = await self.fetch_history(
                        chat_id=data.event.message.chat_id,
                        messages_num=CONFIG["messaging"]["historyPageSize"],
                    )
            except Exception:
                await self._history_failed(data, generation, current_message_id)
                return
            batch = batch_from_chat_history(data, history_items)
            if batch.messages:
                await self.submit(batch, generation)

    async def _history_failed(
        self,
        data: P2ImMessageReceiveV1,
        generation: int,
        current_message_id: str,
    ) -> None:
        """历史请求失败时沿用当前通知和 reaction 清理顺序。"""
        logger.exception("获取历史消息失败: message_id=%s", current_message_id)
        state = ActiveCodexRun(uuid4().hex, data.event.message.chat_id,
                               frozenset({current_message_id}),
                               target_id=(data.event.message.chat_id if data.event.message.chat_type == "group"
                                          else data.event.sender.sender_id.union_id))
        state.probe = RunProbe(state.run_id, state.chat_id, state.message_ids)
        state.probe.finish("failed")
        if generation == self.cache.chat_generations.get(state.chat_id, 0):
            await self.notify(state, "codexFailure")
        await self.clear_reactions(state.chat_id, state.message_ids)
