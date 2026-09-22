"""撤回、停止、新会话以及历史选择和恢复命令。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import NotRequired, TYPE_CHECKING, TypedDict

from fersk_codex.session.session_history import get_session, list_sessions
from fersk_codex.codex.thread_manager import get_user_thread
from fersk_codex.codex.thread_watchdog import settings
from fersk_codex.services.lark import lark_interactive_card as interactive
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger
from .gateway_runtime import GatewayRuntime

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImMessageRecalledV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger, P2CardActionTriggerResponse,
    )

    from fersk_codex.session.session_gateway import ActiveCodexRun
    from fersk_codex.services.lark.lark_interactive_card import HistoryOption
    from fersk_codex.utils.event_dispatcher import EventDispatcher

logger = get_logger("Message")


class HistoryRestoreResult(TypedDict):
    """恢复结果保持原有字典协议，失败时不提供线程 ID。"""

    ok: bool
    content: str
    thread_id: NotRequired[str]


class GatewayCommands:
    """复用 runtime 的停止和提交门禁，独立管理历史卡片上下文。"""

    def __init__(
        self,
        runtime: GatewayRuntime,
        *,
        cancel_buffer: Callable[[str], Awaitable[None]],
    ) -> None:
        self.runtime = runtime
        self.cache = runtime.cache
        self.cancel_buffer = cancel_buffer
        self.history_cards = interactive.HistoryCardStore()

    async def prune_history(self) -> None:
        """供启动层调度历史卡片过期清理。"""
        self.history_cards.prune()

    async def processing_recall(self, data: P2ImMessageRecalledV1) -> None:
        """Interrupt the Codex run associated with an owner-recalled message."""
        event = data.event
        if event.recall_type != "message_owner":
            return

        message_id = event.message_id
        chat_id = event.chat_id
        async with self.cache.active_runs_guard:
            state = self.cache.active_runs_by_message_id.get(message_id)
            if state is None:
                state = next((run for run in self.cache.all_runs.values()
                              if run.chat_id == chat_id and message_id in run.message_ids), None)
            if state is None:
                self.cache.recall(message_id)
            else:
                state.interrupted = True

        async with self.cache.buffer_guard:
            buffered = self.cache.buffered_events.get(chat_id)
            buffered_message = getattr(getattr(buffered, "event", None), "message", None)
            if getattr(buffered_message, "message_id", None) == message_id:
                self.cache.buffered_events.pop(chat_id, None)
                self.cache._buffer_times.pop(chat_id, None)
                task = self.cache.buffer_tasks.pop(chat_id, None)
                if task is not None:
                    task.cancel()
                self.cache.recalled_message_ids.discard(message_id)

        if state is not None:
            if state.probe and not state.probe.stop_reason:
                state.probe.stop_reason = "recalled"
            succeeded = await self.runtime._interrupt_run(state)
            await self.runtime._notify_terminal(state, "recallStopped" if succeeded else "stopFailed")
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                await self.runtime._clear_reaction(chat_id, {message_id})
        except Exception:
            logger.exception("撤回消息 reaction 清理失败: message_id=%s", message_id)
        finally:
            self.cache.release_idle(chat_id)

    async def _stop_chat(
        self,
        data: P2ImMessageReceiveV1 | SimpleNamespace,
        *,
        advance_generation: bool = True,
    ) -> tuple[dict[str, ActiveCodexRun], bool, bool]:
        """停止本会话当前任务和已接收的待处理输入，保留 thread 绑定。"""
        message = data.event.message
        chat_id = message.chat_id
        reaction_ids = set(self.cache.reaction_message_ids.get(chat_id, {}))
        async with self.cache.active_runs_guard:
            if advance_generation:
                self.cache.chat_generations[chat_id] = self.cache.chat_generations.get(chat_id, 0) + 1
            states = {
                state.run_id: state for state in self.cache.active_runs_by_message_id.values()
                if state.chat_id == chat_id
            }
            states.update({state.run_id: state for state in self.cache.all_runs.values()
                           if state.chat_id == chat_id})
            blocked = self.cache.blocked_chats.get(chat_id)
            if blocked:
                blocked.stop_task = None
                blocked.notified = False
                states[blocked.run_id] = blocked
            for state in states.values():
                state.interrupted = True
                if state.probe and not state.probe.stop_reason:
                    state.probe.stop_reason = "stopped"
            had_work = bool(states or self.cache.pending_chat_requests.get(chat_id) or chat_id in self.cache.buffered_events)
        await self.cancel_buffer(chat_id)
        results = await asyncio.gather(*(self.runtime._interrupt_run(state) for state in states.values()))
        succeeded = all(results)
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                await self.runtime._clear_reaction(chat_id, reaction_ids)
        except Exception:
            logger.exception("停止后 reaction 清理失败: chat_id=%s", chat_id)
        return states, succeeded, had_work

    async def processing_stop(self, data: P2ImMessageReceiveV1 | SimpleNamespace) -> None:
        states, succeeded, had_work = await self._stop_chat(data)
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
                await self.runtime.send_card(union_id=target_id, content=CONFIG["messages"][key])
            for state in states.values():
                if state.probe:
                    state.probe.record("notification_sent", messageKey=key)
        except Exception:
            logger.exception("停止卡片投递失败或结果不确定: chat_id=%s", chat_id)

    async def history_options(
        self,
        data: P2ImMessageReceiveV1 | SimpleNamespace,
    ) -> list[HistoryOption]:
        """前端适配入口：data 必须来自已验证的事件，不能由客户端伪造身份。"""
        message = data.event.message
        target_id = message.chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
        return [
            {"label": record.thread_name or "未命名会话", "value": record.thread_id,
             "updated_at": record.updated_at}
            for record in await list_sessions(target_id)
        ]

    async def processing_history(self, data: P2ImMessageReceiveV1 | SimpleNamespace) -> None:
        """仅私聊展示个人历史；不停止任务、不修改活跃绑定。"""
        if data.event.message.chat_type != "p2p":
            return
        user_id = data.event.sender.sender_id.union_id
        if not user_id:
            logger.error("历史卡片缺少可信 union_id")
            return
        card = None
        try:
            # list_sessions 已按 updated_at DESC、thread_id DESC 排序，先排序再限制总数。
            options = (await self.history_options(data))[:CONFIG["messaging"].get("sessionHistoryLimit", 30)]
            card = self.history_cards.create(user_id, data.event.message.chat_id, options)
            card.message_id = await interactive.send_interactive_card(user_id, interactive.build_history_card(card))
        except Exception:
            if card is not None:
                self.history_cards.cards.pop(card.token, None)
            logger.exception("历史卡片加载或发送失败")
            await interactive.send_interactive_card(user_id, interactive.status_card("历史会话加载或发送失败，请重新发送 /history。"))

    async def processing_history_action(self, data: P2CardActionTrigger) -> None:
        """主事件循环内校验、去重并串行恢复；SDK 回调不等待恢复完成。"""
        try:
            card = self.history_cards.resolve(data)
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
                self.history_cards.cards[card.token] = updated
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
            with self.cache.hold(card.chat_id):
                result = await self.processing_history_restore(card.message_event(), thread_id)
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

    def dispatch_history_action(
        self,
        dispatcher: EventDispatcher,
        data: P2CardActionTrigger,
    ) -> P2CardActionTriggerResponse:
        """同步 SDK 入口，只提交已知动作；不把入队成功报告为线程激活成功。"""
        action = getattr(getattr(data, "event", None), "action", None)
        value = getattr(action, "value", None)
        if not isinstance(value, dict) or value.get("action") not in {"activate_history", "history_page"}:
            return interactive.callback_response("未执行任何操作")
        try:
            # 这里只读检查，以便过期/转发卡片得到即时提示；主循环在执行前再次校验。
            self.history_cards.resolve(data)
        except ValueError as exc:
            return interactive.callback_response(str(exc), error=True)
        except AttributeError:
            return interactive.callback_response("卡片回调缺少必要身份或上下文", error=True)
        if not dispatcher.submit(self.processing_history_action, data, control=True):
            return interactive.callback_response("当前任务繁忙，请稍后重试", error=True)
        return interactive.callback_response("正在处理，请以卡片最终结果为准")

    async def processing_history_restore(
        self,
        data: P2ImMessageReceiveV1 | SimpleNamespace,
        thread_id: str,
    ) -> HistoryRestoreResult:
        """返回供前端展示的结果；复用 /new 的停止、等待及提交隔离流程。"""
        chat_id = data.event.message.chat_id
        target_id = chat_id if data.event.message.chat_type == "group" else data.event.sender.sender_id.union_id
        previous = self.cache.reset_tasks.get(chat_id)

        async def restore() -> HistoryRestoreResult:
            try:
                if previous is not None:
                    await asyncio.shield(previous)
                async with asyncio.timeout(settings()["startupTimeoutSeconds"]):
                    if not isinstance(thread_id, str) or not thread_id or await get_session(target_id, thread_id) is None:
                        raise ValueError("历史会话不存在或不属于当前用户")
                    if await get_user_thread(target_id) == thread_id:
                        return {"ok": True, "thread_id": thread_id, "content": "已恢复历史会话"}
                    self.cache.chat_generations[chat_id] = self.cache.chat_generations.get(chat_id, 0) + 1
                    states, succeeded, _ = await self._stop_chat(data, advance_generation=False)
                    for state in states.values():
                        state.notified = True
                    if not succeeded:
                        raise RuntimeError("当前任务停止未确认")
                    for state in states.values():
                        if state.task is not None:
                            await state.finished.wait()
                    async with await self.runtime._get_codex_lock(chat_id):
                        await self.runtime.codex.restore_session(target_id, thread_id)
                return {"ok": True, "thread_id": thread_id, "content": "已恢复历史会话"}
            except Exception:
                logger.exception("恢复历史会话失败: chat_id=%s", chat_id)
                return {"ok": False, "content": "恢复历史会话失败"}
            finally:
                if self.cache.reset_tasks.get(chat_id) is asyncio.current_task():
                    self.cache.reset_tasks.pop(chat_id, None)
                    self.cache.release_idle(chat_id)

        # 第一次 await 之前建立门禁，后续普通输入会等待本次切换完成。
        task = asyncio.create_task(restore())
        self.cache.reset_tasks[chat_id] = task
        return await asyncio.shield(task)

    async def processing_new(self, data: P2ImMessageReceiveV1 | SimpleNamespace) -> None:
        """Gate subsequent submissions before the first await; stop then reset."""
        chat_id = data.event.message.chat_id
        target_id = chat_id if data.event.message.chat_type == "group" else data.event.sender.sender_id.union_id
        previous = self.cache.reset_tasks.get(chat_id)
        self.cache.chat_generations[chat_id] = self.cache.chat_generations.get(chat_id, 0) + 1

        async def reset() -> None:
            try:
                if previous is not None:
                    await asyncio.shield(previous)
                states, succeeded, _ = await self._stop_chat(data, advance_generation=False)
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
                            async with await self.runtime._get_codex_lock(chat_id):
                                await self.runtime.codex.reset_thread(target_id)
                        key = "newThreadCreated"
                    except Exception:
                        logger.exception("新会话重置失败: chat_id=%s", chat_id)
                        key = "newThreadFailed"
                async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                    await self.runtime.send_card(union_id=target_id, content=CONFIG["messages"][key])
            except Exception:
                logger.exception("新会话命令处理或通知失败: chat_id=%s", chat_id)
            finally:
                if self.cache.reset_tasks.get(chat_id) is asyncio.current_task():
                    self.cache.reset_tasks.pop(chat_id, None)
                    self.cache.release_idle(chat_id)

        task = asyncio.create_task(reset())
        self.cache.reset_tasks[chat_id] = task
        await asyncio.shield(task)
