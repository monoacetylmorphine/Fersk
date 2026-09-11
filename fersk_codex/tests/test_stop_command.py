"""验证 /stop、撤回和启动竞态；模拟飞书及 Codex，不读取凭据。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from fersk_codex.core.codex import FerskCodex
from fersk_codex.core import codex
from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.middleware import message_router
from fersk_codex.middleware.message_collector import batch_from_chat_history, is_stop_command


def event(text="/stop", chat="chat-1", message_id="stop-1", chat_type="p2p"):
    return NS(event=NS(message=NS(
        chat_id=chat, chat_type=chat_type, message_id=message_id,
        message_type="text", content=json.dumps({"text": text}), mentions=[],
    ), sender=NS(sender_id=NS(union_id="user-1"))))


def history(text, message_id):
    return {"message_id": message_id, "msg_type": "text",
            "sender": {"sender_type": "user"},
            "body": {"content": json.dumps({"text": text})}}


class CommandTests(unittest.TestCase):
    def test_exact_command_and_configuration(self):
        for text in ("/stop", "  /STOP\n"):
            self.assertTrue(is_stop_command("text", json.dumps({"text": text})))
        for text in ("请 /stop", "/stop now", "/stopping", "@_user_1 /stop"):
            self.assertFalse(is_stop_command("text", json.dumps({"text": text})))
        for raw in ("invalid", "[]", "null", '{"text": 1}'):
            self.assertFalse(is_stop_command("text", raw))
        self.assertFalse(is_stop_command("post", '{"text": "/stop"}'))
        with patch.dict(CONFIG["messaging"], stopThreadCommand="/halt"):
            self.assertTrue(is_stop_command("text", '{"text": "/HALT"}'))
            self.assertFalse(is_stop_command("text", '{"text": "/stop"}'))

    def test_configured_stop_command_excludes_event_and_bounds_history(self):
        with patch.dict(CONFIG["messaging"], stopThreadCommand="/halt"):
            self.assertFalse(batch_from_chat_history(event(" /HALT "), []).messages)
            items = [history("hello", "current"), history(" /HALT ", "halt"), history("old", "old")]
            result = batch_from_chat_history(event("hello", message_id="current"), items)
            self.assertEqual([message.message_id for message in result.messages], ["current"])
            # The previous default now belongs to ordinary user input.
            result = batch_from_chat_history(event("/stop"), [])
            self.assertEqual([message.content for message in result.messages], [{"text": "/stop"}])

    def test_stop_is_excluded_and_bounds_history(self):
        items = [history("hello", "new"), history(" /STOP ", "stop"), history("old", "old")]
        batch = batch_from_chat_history(event("hello", message_id="new"), items)
        self.assertEqual([m.message_id for m in batch.messages], ["new"])
        self.assertFalse(batch_from_chat_history(event(), []).messages)

    def test_new_command_still_stands_alone_and_bounds_history(self):
        batch = batch_from_chat_history(event(" /NEW ", message_id="new"), [history("old", "old")])
        self.assertFalse(batch.messages)
        batch = batch_from_chat_history(event("hello", message_id="next"),
                                       [history("/new", "new"), history("old", "old")])
        self.assertEqual([m.message_id for m in batch.messages], ["next"])


class StopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("fersk_codex.core.thread_watchdog.journal.record"))
        def module(name, **values):
            result = ModuleType(name)
            result.__dict__.update(values)
            return result
        with patch.dict(sys.modules, {"fersk_codex.services.lark.lark_client": module("fersk_codex.services.lark.lark_client", client=NS())}):
            from fersk_codex.services.lark.lark_card import CardDeliveryError, CardReplace, CardSteer, CardStreamSession, CardStreamStopped
            self.card_module = sys.modules["fersk_codex.services.lark.lark_card"]
            self.card_control_type = CardSteer
        # 网关全部外部 I/O 替换，单独运行也不会读取凭据。
        stubs = {
            "fersk_codex.services.lark.lark_client": module("fersk_codex.services.lark.lark_client", create_websocket_client=None),
            "fersk_codex.services.lark.lark_tools": module("fersk_codex.services.lark.lark_tools",
                getting_chat_history=AsyncMock(return_value=[]),
                adding_reaction_emoji=AsyncMock(return_value="reaction-1"),
                delete_reaction_emoji=AsyncMock(return_value=True)),
            "fersk_codex.services.lark.lark_card": module("fersk_codex.services.lark.lark_card", CardReplace=CardReplace,
                CardStreamSession=CardStreamSession, CardSteer=CardSteer,
                CardDeliveryError=CardDeliveryError, CardStreamStopped=CardStreamStopped,
                sending_card=AsyncMock()),
            "fersk_codex.middleware.message_assemble": module("fersk_codex.middleware.message_assemble",
                InputAssemblyError=type("InputAssemblyError", (Exception,), {}),
                assemble_codex_input=AsyncMock(return_value=NS(codex_input="hello", notices=()))),
        }
        spec = importlib.util.spec_from_file_location("gateway_stop_tests", (Path(__file__).resolve().parents[1] / "gateway.py"))
        self.g = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {**stubs, spec.name: self.g}):
            spec.loader.exec_module(self.g)
        async def running(**kwargs):
            yield {"type": "started", "thread_id": "thread-1", "turn_id": "turn-1"}
            yield {"type": "done"}
        self.g.FerskCodex = NS(interrupt_and_confirm=AsyncMock(return_value=True), force_close=AsyncMock(return_value=True), completed_status=AsyncMock(return_value=None), forget_run=AsyncMock(),
                              reset_thread=AsyncMock(), running=running, steer=AsyncMock(return_value={"type": "idle"}))

    def state(self, chat="chat-1", started=True):
        state = self.g.ActiveCodexRun("run-" + chat, chat, frozenset({"m1", "m2"}), codex_started=started)
        for mid in state.message_ids:
            self.g.active_runs_by_message_id[chat + mid] = state
        return state

    async def test_stop_bypasses_lock_and_deduplicates_batch(self):
        state = self.state()
        other = self.state("chat-2")
        lock = await self.g._get_codex_lock("chat-1")
        async with lock:
            await asyncio.wait_for(self.g.processing(event(" /STOP ")), 1)
        self.assertTrue(state.interrupted)
        self.assertFalse(other.interrupted)
        self.g.FerskCodex.interrupt_and_confirm.assert_awaited_once_with(state.run_id)
        self.g.getting_chat_history.assert_not_awaited()
        self.g.adding_reaction_emoji.assert_not_awaited()
        self.assertEqual(self.g.sending_card.call_args.kwargs["content"], CONFIG["messages"]["stopRequested"])

    async def test_stop_before_turn_start(self):
        state = self.state(started=False)
        await self.g.processing(event())
        self.assertFalse(await self.g._start_codex_run(state))
        self.g.FerskCodex.interrupt_and_confirm.assert_not_awaited()

    async def test_stop_closes_reply_stream(self):
        state = self.state()
        closed = []
        async def events(**kwargs):
            try:
                yield {"type": "answer", "content": "已显示"}
                yield {"type": "answer", "content": "停止后内容"}
            finally:
                closed.append(True)
        self.g.FerskCodex.running = events
        stream = self.g._reply_content(NS(union_id="user-1"), "hello", state)
        await anext(stream)
        await self.g.processing(event())
        with self.assertRaises(self.g.CardStreamStopped):
            await anext(stream)
        self.assertEqual(closed, [True])

    async def test_stop_during_reaction_does_not_submit_old_message(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        async def reaction(**kwargs):
            entered.set()
            await resume.wait()
            return "late-reaction"
        self.g.adding_reaction_emoji.side_effect = reaction
        task = asyncio.create_task(self.g.processing(event("hello", message_id="old")))
        await entered.wait()
        await self.g.processing(event())
        resume.set()
        await task
        self.g.getting_chat_history.assert_not_awaited()
        self.g.delete_reaction_emoji.assert_awaited_once_with(message_id="old", reaction_id="late-reaction")

    async def test_repeat_stop_and_idle(self):
        await self.g.processing(event())
        self.assertEqual(self.g.sending_card.call_args.kwargs["content"], CONFIG["messages"]["stopIdle"])
        state = self.state()
        await self.g.processing(event(message_id="stop-2"))
        await self.g.processing(event(message_id="stop-3"))
        self.assertTrue(state.interrupted)
        self.assertEqual(self.g.FerskCodex.interrupt_and_confirm.await_count, 1)

    async def test_interrupt_failure_keeps_output_closed(self):
        state = self.state()
        self.g.FerskCodex.interrupt_and_confirm.side_effect = RuntimeError("offline")
        await self.g.processing(event())
        self.assertTrue(await self.g._run_was_interrupted(state))
        self.assertEqual(self.g.sending_card.call_args.kwargs["content"], CONFIG["messages"]["stopFailed"])

    async def test_recall_uses_same_interrupt_and_owner_filter(self):
        state = self.state()
        self.g.active_runs_by_message_id["m1"] = state
        recalled = NS(event=NS(message_id="m1", chat_id="chat-1", recall_type="admin"))
        await self.g.processing_recall(recalled)
        self.assertFalse(state.interrupted)
        recalled.event.recall_type = "message_owner"
        await self.g.processing_recall(recalled)
        self.assertTrue(state.interrupted)
        self.g.FerskCodex.interrupt_and_confirm.assert_awaited_once_with(state.run_id)

    async def test_buffer_cancel_and_next_message_works(self):
        data = event("image")
        data.event.message.message_type = "image"
        await self.g.router._buffer_message(data, 0)
        task = self.g.buffer_tasks["chat-1"]
        await self.g.processing(event())
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("chat-1", self.g.buffer_tasks)
        self.assertNotIn("chat-1", self.g.buffered_events)
        self.g._handle_message_batch = AsyncMock()
        await self.g.processing(event("hello", message_id="next"))
        self.g._handle_message_batch.assert_awaited_once()
        self.assertEqual(self.g._handle_message_batch.call_args.args[1], 0)
        self.assertNotIn("chat-1", self.g.chat_generations)

    async def test_stop_during_history_fetch_discards_old_request(self):
        fetching, resume = asyncio.Event(), asyncio.Event()
        async def fetch(**kwargs):
            fetching.set()
            await resume.wait()
            return []
        self.g.getting_chat_history.side_effect = fetch
        task = asyncio.create_task(self.g.processing(event("hello", message_id="old")))
        await fetching.wait()
        await self.g.processing(event())
        resume.set()
        await task
        self.g.assemble_codex_input.assert_not_awaited()

    async def test_stop_discards_request_waiting_for_lock(self):
        batch = batch_from_chat_history(event("hello", message_id="old"), [])
        lock = await self.g._get_codex_lock("chat-1")
        async with lock:
            task = asyncio.create_task(self.g._handle_message_batch(batch, 0))
            await asyncio.sleep(0)
            await self.g.processing(event())
        await task
        self.g.assemble_codex_input.assert_not_awaited()

    async def test_group_requires_mention_and_replies_to_chat(self):
        data = event(chat_type="group")
        with patch("fersk_codex.middleware.message_router._is_bot_mentioned", return_value=False):
            await self.g.processing(data)
        self.g.sending_card.assert_not_awaited()
        with patch("fersk_codex.middleware.message_router._is_bot_mentioned", return_value=True):
            await self.g.processing(data)
        self.assertEqual(self.g.sending_card.call_args.kwargs["union_id"], "chat-1")

    async def test_multimodal_batch_clears_every_reaction(self):
        self.g.message_buffer_seconds = 0
        for text_finishes in (False, True):
            with self.subTest(text_finishes=text_finishes):
                self.g.received_message_ids.clear()
                self.g.processed_message_ids.clear()
                self.g.adding_reaction_emoji.side_effect = ["r1", "r2", "r3"]
                self.g.delete_reaction_emoji.reset_mock()
                items = []
                for index in range(1, 4):
                    mid = f"m{index}"
                    data = event("describe", message_id=mid)
                    kind = "text" if text_finishes and index == 3 else "image"
                    data.event.message.message_type = kind
                    item = history("describe", mid)
                    item["msg_type"] = kind
                    items.insert(0, item)
                    self.g.getting_chat_history.return_value = list(items)
                    await self.g.processing(data)
                if not text_finishes:
                    # 实际执行定时 flush 路径，缩短窗口以避免测试等待。
                    await self.g.buffer_tasks["chat-1"]
                self.assertEqual(
                    {tuple(call.kwargs.values()) for call in self.g.delete_reaction_emoji.await_args_list},
                    {("m1", "r1"), ("m2", "r2"), ("m3", "r3")},
                )
                self.assertEqual(self.g.delete_reaction_emoji.await_count, 3)
                self.assertFalse(self.g.reaction_message_ids)

    async def test_batch_cleanup_preserves_later_message_and_other_chat(self):
        self.g.reaction_message_ids = {
            "chat-1": {"m1": "r1", "m2": "r2", "next": "r3"},
            "chat-2": {"other": "r4"},
        }
        batch = batch_from_chat_history(event("hello", message_id="m2"),
                                       [history("hello", "m2"), history("hello", "m1")])
        await self.g._handle_message_batch(batch, 0)
        self.assertEqual(self.g.delete_reaction_emoji.await_count, 2)
        self.assertEqual(self.g.reaction_message_ids,
                         {"chat-1": {"next": "r3"}, "chat-2": {"other": "r4"}})

    async def test_stop_clears_snapshot_and_preserves_new_reaction(self):
        self.state()
        self.g.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2"}}
        async def interrupt(run_id):
            self.g.reaction_message_ids["chat-1"]["next"] = "r3"
            return True
        self.g.FerskCodex.interrupt_and_confirm.side_effect = interrupt
        await self.g.processing(event())
        self.assertEqual(self.g.delete_reaction_emoji.await_count, 2)
        self.assertEqual(self.g.reaction_message_ids, {"chat-1": {"next": "r3"}})

    async def test_recall_then_batch_cleanup_is_idempotent(self):
        state = self.state()
        self.g.active_runs_by_message_id["m1"] = state
        self.g.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2"}}
        await self.g.processing_recall(NS(event=NS(
            message_id="m1", chat_id="chat-1", recall_type="message_owner")))
        self.assertEqual(self.g.reaction_message_ids, {"chat-1": {"m2": "r2"}})
        await self.g._clear_reaction("chat-1", state.message_ids)
        await self.g._clear_reaction("chat-1", state.message_ids)
        self.assertEqual(self.g.delete_reaction_emoji.await_count, 2)
        self.assertFalse(self.g.reaction_message_ids)

    async def test_failed_delete_retains_record_and_continues(self):
        self.g.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2", "m3": "r3"}}
        self.g.delete_reaction_emoji.side_effect = [False, RuntimeError("offline"), True]
        await self.g._clear_reaction("chat-1")
        self.assertEqual(self.g.delete_reaction_emoji.await_count, 3)
        self.assertEqual(self.g.reaction_message_ids, {"chat-1": {"m1": "r1", "m2": "r2"}})
        self.assertFalse(self.g.reactions_being_cleared)
        self.g.delete_reaction_emoji.side_effect = None
        await self.g._clear_reaction("chat-1")
        self.assertFalse(self.g.reaction_message_ids)

    async def test_concurrent_cleanup_does_not_delete_twice(self):
        self.g.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2"}}
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delete(message_id, reaction_id):
            if message_id == "m1":
                entered.set()
                await resume.wait()
            return True
        self.g.delete_reaction_emoji.side_effect = delete
        task = asyncio.create_task(self.g._clear_reaction("chat-1"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.g._clear_reaction("chat-1")
        finally:
            resume.set()
            await task
        self.assertEqual(self.g.delete_reaction_emoji.await_count, 2)
        self.assertFalse(self.g.reaction_message_ids)
        self.assertFalse(self.g.reactions_being_cleared)

    async def test_failed_add_does_not_store_invalid_reaction(self):
        self.g.adding_reaction_emoji.return_value = None
        await self.g.processing(event("hello", message_id="m1"))
        self.g.assemble_codex_input.assert_awaited_once()
        self.g.delete_reaction_emoji.assert_not_awaited()
        self.assertFalse(self.g.reaction_message_ids)

    async def test_batch_failure_still_clears_all_reactions(self):
        self.g.reaction_message_ids = {"chat-1": {"m1": "r1", "m2": "r2"}}
        self.g.assemble_codex_input.side_effect = RuntimeError("assembly failed")
        batch = batch_from_chat_history(event("hello", message_id="m2"),
                                       [history("hello", "m2"), history("hello", "m1")])
        await self.g._handle_message_batch(batch, 0)
        self.assertEqual(self.g.delete_reaction_emoji.await_count, 2)
        self.assertFalse(self.g.reaction_message_ids)


class BackendInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_arriving_during_turn_start_is_delivered(self):
        starting, resume = asyncio.Event(), asyncio.Event()
        async def events():
            yield NS(method="item/agentMessage/delta", payload=NS(delta="answer", item_id="a"))
        handle = NS(interrupt=AsyncMock(), stream=events)
        async def turn(**kwargs):
            starting.set()
            await resume.wait()
            return handle
        with (
            patch.object(FerskCodex, "_active_turns", {}),
            patch.object(FerskCodex, "_pending_interrupts", set()),
            patch.object(FerskCodex, "_turns_guard", asyncio.Lock()),
            patch.object(codex, "get_user_thread", AsyncMock(return_value=None)),
            patch.object(codex, "set_user_thread", AsyncMock()),
            patch.object(codex, "prepare_workspace", AsyncMock()),
            patch.object(codex, "AsyncCodex") as client_class,
        ):
            client = client_class.return_value.__aenter__.return_value
            client.thread_start.return_value = NS(id="thread-1", turn=turn)
            stream = FerskCodex.running("user-1", "hello", run_id="run")
            task = asyncio.create_task(anext(stream))
            try:
                await asyncio.wait_for(starting.wait(), 1)
                self.assertFalse(await FerskCodex.interrupt("run"))
                resume.set()
                await asyncio.wait_for(task, 1)
                handle.interrupt.assert_awaited_once()
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await stream.aclose()
            self.assertFalse(FerskCodex._active_turns)
            self.assertFalse(FerskCodex._pending_interrupts)

    async def test_pending_interrupt_and_handle_cleanup(self):
        with patch.object(FerskCodex, "_active_turns", {}), patch.object(FerskCodex, "_pending_interrupts", set()), patch.object(FerskCodex, "_turns_guard", asyncio.Lock()):
            self.assertFalse(await FerskCodex.interrupt("run"))
            self.assertIn("run", FerskCodex._pending_interrupts)
            handle = NS(interrupt=AsyncMock())
            FerskCodex._active_turns["run"] = handle
            self.assertTrue(await FerskCodex.interrupt("run"))
            handle.interrupt.assert_awaited_once()
            await FerskCodex.forget_run("run")
            self.assertFalse(FerskCodex._pending_interrupts)
            self.assertFalse(FerskCodex._active_turns)


if __name__ == "__main__":
    unittest.main()
