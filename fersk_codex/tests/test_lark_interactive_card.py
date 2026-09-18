"""私聊历史卡片离线回归：真实 SDK 数据模型，外部请求和恢复操作模拟。"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from fersk_codex.services.lark import lark_interactive_card as cards
from fersk_codex.middleware.message_collector import batch_from_chat_history, is_history_command
from fersk_codex.core import codex, session_history, thread_manager
import test_stop_command as helpers


def callback(card, *, selected="thread-1", action="activate_history", user="user-1", **changes):
    payload = {
        "operator": {"union_id": user},
        "context": {"open_chat_id": card.chat_id, "open_message_id": card.message_id},
        "action": {"tag": "button", "name": "activate_history",
                   "value": {"action": action, "ticket": card.token, "revision": card.revision},
                   "form_value": {cards.SELECT_NAME: selected}},
    }
    payload.update(changes)
    return P2CardActionTrigger({"event": payload})


def options(count=1):
    return [{"label": "同名会话", "value": f"thread-{index + 1}", "updated_at": None}
            for index in range(count)]


class InteractiveCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = cards.HistoryCardStore()
        self.card = self.store.create("user-1", "chat-1", options(cards.PAGE_SIZE + 3))
        self.card.message_id = "message-1"

    def test_form_requires_selection_and_confirmation(self):
        body = cards.build_history_card(self.card)
        self.assertEqual(body["schema"], "2.0")
        form = body["body"]["elements"][1]
        select, button = form["elements"]
        self.assertEqual(form["tag"], "form")
        self.assertTrue(select["required"])
        self.assertNotIn("behaviors", select)  # 单纯选择不回调。
        self.assertNotIn("initial_option", select)
        self.assertEqual(len(select["options"]), cards.PAGE_SIZE)
        self.assertEqual(len({item["value"] for item in select["options"]}), cards.PAGE_SIZE)
        self.assertEqual(button["form_action_type"], "submit")
        self.assertNotIn("action_type", button)
        self.assertIn("confirm", button)
        self.assertIn("停止当前", button["confirm"]["text"]["content"])
        self.card.page = 1
        self.assertEqual(len(cards.build_history_card(self.card)["body"]["elements"][1]["elements"][0]["options"]), 3)

    def test_empty_history_has_no_controls(self):
        self.card.options = ()
        self.assertNotIn("select_static", json.dumps(cards.build_history_card(self.card)))

    def test_identity_scope_expiry_and_replay(self):
        self.assertIs(self.store.resolve(callback(self.card)), self.card)
        for data in (callback(self.card, user="other"), callback(self.card, user=None),
                     callback(self.card, context={"open_chat_id": "group", "open_message_id": "message-1"}),
                     callback(self.card, context={"open_chat_id": "chat-1", "open_message_id": "forwarded"})):
            with self.assertRaises(ValueError):
                self.store.resolve(data)
        self.card.finished = True
        with self.assertRaises(ValueError):
            self.store.resolve(callback(self.card))
        self.card.finished = False
        self.card.created_at -= cards.CARD_TTL_SECONDS
        with self.assertRaises(ValueError):
            self.store.resolve(callback(self.card))

    def test_only_confirmed_visible_form_option_is_accepted(self):
        self.assertEqual(cards.confirmed_thread_id(callback(self.card), self.card), "thread-1")
        for selected in (None, [], "foreign", f"thread-{cards.PAGE_SIZE + 1}"):
            with self.assertRaises(ValueError):
                cards.confirmed_thread_id(callback(self.card, selected=selected), self.card)
        data = callback(self.card)
        data.event.action.tag = "select_static"
        data.event.action.option = "thread-1"
        with self.assertRaises(ValueError):
            cards.confirmed_thread_id(data, self.card)

    async def test_send_and_update_use_real_sdk_requests(self):
        client = NS(im=NS(v1=NS(message=NS(create=Mock(), patch=Mock()))))
        stub = ModuleType("fersk_codex.services.lark.lark_client")
        stub.client = client
        call = AsyncMock(return_value=NS(success=lambda: True, data=NS(message_id="sent")))
        with patch.dict(sys.modules, {stub.__name__: stub}), patch.object(cards, "call_lark", call):
            body = cards.build_history_card(self.card)
            self.assertEqual(await cards.send_interactive_card("user-1", body), "sent")
            request = call.call_args.args[1]
            self.assertIn(("receive_id_type", "union_id"), request.queries)
            self.assertEqual(request.request_body.receive_id, "user-1")
            self.assertEqual(json.loads(request.request_body.content), body)
            await cards.update_interactive_card("sent", cards.status_card("已激活"))
            self.assertEqual(call.call_args.args[1].message_id, "sent")
            call.return_value = NS(success=lambda: False, code=123, get_log_id=lambda: "log")
            with self.assertRaisesRegex(RuntimeError, "123"):
                await cards.send_interactive_card("user-1", body)


class HistoryInteractionGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.StopTests.setUp(self)
        self.send = self.enterContext(patch.object(cards, "send_interactive_card", AsyncMock(return_value="message-1")))
        self.update = self.enterContext(patch.object(cards, "update_interactive_card", AsyncMock()))
        self.listing = self.enterContext(patch.object(self.g, "list_sessions", AsyncMock(return_value=[
            NS(thread_id="thread-1", thread_name="历史名称", updated_at=None)])))
        self.real_restore = self.g.processing_history_restore
        self.restore = self.enterContext(patch.object(self.g, "processing_history_restore", AsyncMock(
            return_value={"ok": True, "thread_id": "thread-1"})))
        self.card = self.g.history_cards.create("user-1", "chat-1", options())
        self.card.message_id = "message-1"

    async def test_private_command_bypasses_model_and_group_unchanged(self):
        await self.g.processing(helpers.event(" /HISTORY "))
        self.listing.assert_awaited_once_with("user-1")
        self.send.assert_awaited_once()
        self.g.getting_chat_history.assert_not_awaited()
        self.g.adding_reaction_emoji.assert_not_awaited()
        self.restore.assert_not_awaited()
        self.send.reset_mock()
        await self.g.processing_history(helpers.event("/history", chat_type="group"))
        self.send.assert_not_awaited()
        route = AsyncMock()
        with patch.object(self.g.router, "_route_message", route), patch(
                "fersk_codex.middleware.message_router._is_bot_mentioned", return_value=True):
            await self.g.processing(helpers.event("/history", chat_type="group", message_id="group-1"))
        route.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_confirm_activates_once_with_trusted_context(self):
        data = callback(self.card)
        await self.g.processing_history_action(data)
        await self.g.processing_history_action(data)
        self.restore.assert_awaited_once()
        trusted, thread_id = self.restore.call_args.args
        self.assertEqual(trusted.event.message.chat_type, "p2p")
        self.assertEqual(trusted.event.message.chat_id, "chat-1")
        self.assertEqual(trusted.event.sender.sender_id.union_id, "user-1")
        self.assertEqual(thread_id, "thread-1")
        self.update.assert_awaited_once()
        self.assertIn("已激活", json.dumps(self.update.call_args.args[1], ensure_ascii=False))

    async def test_cancel_unknown_select_and_foreign_callbacks_do_not_restore(self):
        dispatcher = Mock()
        reply = self.g.dispatch_history_action(dispatcher, callback(self.card, action="cancel"))
        self.assertIn("未执行", reply.toast.content)
        dispatcher.submit.assert_not_called()
        for data in (callback(self.card, user="other"), callback(self.card, selected="foreign")):
            await self.g.processing_history_action(data)
        data = callback(self.card)
        data.event.action.tag = "select_static"
        await self.g.processing_history_action(data)
        self.restore.assert_not_awaited()
        self.assertFalse(self.card.finished)

    async def test_failure_consumes_confirmation_and_delivery_failure_does_not_restore_twice(self):
        self.restore.return_value = {"ok": False}
        self.update.side_effect = RuntimeError("network")
        data = callback(self.card)
        await self.g.processing_history_action(data)
        await self.g.processing_history_action(data)
        self.restore.assert_awaited_once()
        self.assertIn("激活失败", json.dumps(self.send.call_args.args[1], ensure_ascii=False))
        self.assertTrue(self.card.finished)
        self.assertFalse(self.card.busy)

    async def test_concurrent_confirmation_is_not_replayed(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def restore(*args):
            started.set()
            await release.wait()
            return {"ok": True}
        self.restore.side_effect = restore
        task = asyncio.create_task(self.g.processing_history_action(callback(self.card)))
        await started.wait()
        await self.g.processing_history_action(callback(self.card))
        release.set()
        await task
        self.restore.assert_awaited_once()

    async def test_pagination_rejects_old_revision_and_does_not_restore(self):
        self.card.options = tuple(options(cards.PAGE_SIZE + 1))
        data = callback(self.card, action="history_page")
        data.event.action.value["page"] = 1
        await self.g.processing_history_action(data)
        updated = self.g.history_cards.cards[self.card.token]
        self.assertEqual(updated.page, 1)
        self.assertEqual(updated.revision, 1)
        await self.g.processing_history_action(data)
        self.update.assert_awaited_once()
        self.restore.assert_not_awaited()

    async def test_query_error_not_reported_as_empty_history(self):
        self.listing.side_effect = RuntimeError("database")
        await self.g.processing_history(helpers.event("/history"))
        text = json.dumps(self.send.call_args.args[1], ensure_ascii=False)
        self.assertIn("失败", text)
        self.assertNotIn("暂无", text)

    async def test_history_card_limits_sorted_database_records_without_deleting_history(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(session_history, "DB_PATH", Path(directory) / "state.sqlite"))
        self.enterContext(patch.object(self.g, "list_sessions", session_history.list_sessions))
        expected = []
        # 故意乱序插入、制造同秒和空时间，验证按数据库更新时间排序后再截取。
        for index in range(36):
            thread_id = f"history-{index:02d}"
            updated_at = (index * 7) % 10 if index else None
            await session_history.register_session("user-1", thread_id, "同名会话")
            if updated_at is not None:
                await session_history.update_session_time("user-1", thread_id, updated_at)
            expected.append((updated_at if updated_at is not None else -1, thread_id))
        await session_history.register_session("other", "foreign", "其他用户")
        await session_history.update_session_time("other", "foreign", 999)
        expected_ids = [item[1] for item in sorted(expected, reverse=True)]
        for limit in (30, 5, 40):
            with self.subTest(limit=limit), patch.dict(self.g.CONFIG["messaging"], sessionHistoryLimit=limit), patch.object(cards, "PAGE_SIZE", limit):
                await self.g.processing_history(helpers.event("/history"))
                body = self.send.call_args.args[1]
                values = body["body"]["elements"][1]["elements"][0]["options"]
                self.assertEqual([item["value"] for item in values], expected_ids[:limit])
                self.assertNotIn("下一页", json.dumps(body, ensure_ascii=False))
        legacy = dict(self.g.CONFIG["messaging"])
        legacy.pop("sessionHistoryLimit")
        with patch.dict(self.g.CONFIG["messaging"], legacy, clear=True):
            await self.g.processing_history(helpers.event("/history"))
        self.assertEqual(len(self.send.call_args.args[1]["body"]["elements"][1]["elements"][0]["options"]), 30)
        self.assertEqual(len(await session_history.list_sessions("user-1")), 36)

    async def test_confirm_to_real_database_and_unarchive_failure_preserves_binding(self):
        directory = self.enterContext(TemporaryDirectory())
        db_path = Path(directory) / "state.sqlite"
        self.enterContext(patch.object(session_history, "DB_PATH", db_path))
        self.enterContext(patch.object(thread_manager, "DB_PATH", db_path))
        await session_history.register_session("user-1", "thread-1", "历史名称")
        await session_history.update_session_time("user-1", "thread-1", 100)
        sdk = NS(thread_unarchive=AsyncMock(return_value=NS(
            id="thread-1", read=AsyncMock(return_value=NS(thread=NS(updated_at=200))))))

        @asynccontextmanager
        async def session(run_id):
            yield sdk

        self.enterContext(patch.object(codex.FerskCodex, "_session", session))
        self.g.FerskCodex.restore_session = codex.FerskCodex.restore_session
        self.enterContext(patch.object(self.g, "processing_history_restore", self.real_restore))
        for failure in ("unarchive", "database", None):
            with self.subTest(failure=failure):
                await thread_manager.set_user_thread("user-1", "old")
                self.card.finished = False
                sdk.thread_unarchive.reset_mock()
                sdk.thread_unarchive.side_effect = RuntimeError("unarchive") if failure == "unarchive" else None
                binding = (patch.object(codex, "set_user_thread", AsyncMock(side_effect=OSError("disk")))
                           if failure == "database" else nullcontext())
                with binding:
                    await self.g.processing_history_action(callback(self.card))
                sdk.thread_unarchive.assert_awaited_once_with("thread-1")
                self.assertEqual(await thread_manager.get_user_thread("user-1"), "old" if failure else "thread-1")
                self.assertNotIn("chat-1", self.g.reset_tasks)
                text = json.dumps(self.update.call_args.args[1], ensure_ascii=False)
                self.assertIn("激活失败" if failure else "已激活", text)

    def test_callback_capacity_and_expiry_return_error_toast(self):
        dispatcher = Mock()
        dispatcher.submit.return_value = False
        self.assertEqual(self.g.dispatch_history_action(dispatcher, callback(self.card)).toast.type, "error")
        dispatcher.submit.reset_mock()
        self.card.created_at -= cards.CARD_TTL_SECONDS
        self.assertIn("失效", self.g.dispatch_history_action(dispatcher, callback(self.card)).toast.content)
        dispatcher.submit.assert_not_called()


class HistoryCommandTests(unittest.TestCase):
    def test_exact_command_and_private_history_boundary(self):
        self.assertTrue(is_history_command("text", '{"text":" /HISTORY "}'))
        for raw in ("[]", "null", "bad", '{"text":"please /history"}'):
            self.assertFalse(is_history_command("text", raw))
        self.assertFalse(batch_from_chat_history(helpers.event("/history"), []).messages)
        items = [helpers.history("hello", "next"), helpers.history("/history", "history"), helpers.history("old", "old")]
        batch = batch_from_chat_history(helpers.event("hello", message_id="next"), items)
        self.assertEqual([item.message_id for item in batch.messages], ["next"])
        batch = batch_from_chat_history(helpers.event("/history", chat_type="group"), [])
        self.assertEqual(batch.messages[0].content, {"text": "/history"})
