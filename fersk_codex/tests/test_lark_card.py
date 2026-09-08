"""验证卡片请求及网关流式适配；不加载真实凭据、不访问飞书。"""
import ast
import asyncio
import importlib
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import aclosing


def response(**data):
    return SimpleNamespace(success=lambda: True, data=SimpleNamespace(**data))


class LarkCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
                create=Mock(return_value=response(message_id="message-1")),
            ))),
            cardkit=SimpleNamespace(v1=SimpleNamespace(
                card=SimpleNamespace(
                    create=Mock(return_value=response(card_id="card-1")),
                    settings=Mock(return_value=response()),
                ),
                card_element=SimpleNamespace(content=Mock(return_value=response())),
            )),
        )
        # 仅替换项目的鉴权入口，请求对象使用真正的 Lark SDK。
        stub = ModuleType("fersk_codex.services.lark.lark_client")
        stub.client = self.client
        with patch.dict(sys.modules, {"fersk_codex.services.lark.lark_client": stub}):
            self.card = importlib.import_module("fersk_codex.services.lark.lark_card")
        self.client_patch = patch.object(self.card, "client", self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def use_async_api(self, after_call=None):
        """Deterministic offline API for timer tests, without worker-thread jitter."""
        async def call(operation, request):
            result = operation(request)
            if after_call:
                after_call(operation, request)
            if not result.success():
                raise self.card.CardRequestError(result.msg, code=result.code)
            return result
        self.enterContext(patch.object(self.card, "_call", side_effect=call))

    async def test_static_card_and_recipient_routing(self):
        for recipient, kind in [("on_user", "union_id"), ("oc_group", "chat_id")]:
            self.assertEqual(await self.card.sending_card(recipient, "**你好**"), "message-1")
            request = self.client.im.v1.message.create.call_args.args[0]
            self.assertIn(("receive_id_type", kind), request.queries)
            self.assertEqual(request.request_body.msg_type, "interactive")
            body = json.loads(request.request_body.content)
            self.assertEqual(body["schema"], "2.0")
            self.assertEqual(body["header"]["template"], "blue")
            self.assertFalse(body["config"]["streaming_mode"])
            self.assertEqual(body["body"]["elements"][0]["content"], "**你好**")
        self.client.cardkit.v1.card.create.assert_not_called()

    async def test_stream_sends_before_end_and_flushes_tail(self):
        async def chunks():
            yield "你"
            self.client.im.v1.message.create.assert_called_once()
            yield "好"
            self.client.cardkit.v1.card_element.content.assert_called_once()
            yield "！"
        with patch.object(self.card, "UPDATE_INTERVAL", 0):
            result = await self.card.sending_card("on_user", chunks())
        self.assertEqual(result, "message-1")
        writes = self.client.cardkit.v1.card_element.content.call_args_list
        self.assertEqual([c.args[0].request_body.content for c in writes], ["你好", "你好！"])
        close = self.client.cardkit.v1.card.settings.call_args.args[0].request_body
        self.assertEqual([c.args[0].request_body.sequence for c in writes] + [close.sequence], [1, 2, 3])
        self.assertFalse(json.loads(close.settings)["config"]["streaming_mode"])
        self.assertEqual(len({c.args[0].request_body.uuid for c in writes} | {close.uuid}), 3)

    async def test_coalescing_keeps_final_text(self):
        async def chunks():
            for chunk in ["a", "b", "c"]:
                yield chunk
        with patch.object(self.card, "UPDATE_INTERVAL", 999):
            await self.card.sending_card("on_user", chunks())
        write = self.client.cardkit.v1.card_element.content
        write.assert_called_once()
        self.assertEqual(write.call_args.args[0].request_body.content, "abc")

    async def test_empty_stream_creates_no_message(self):
        async def chunks():
            yield ""
        self.assertIsNone(await self.card.sending_card("on_user", chunks()))
        self.assertIsNone(await self.card.sending_card("on_user", ""))
        self.client.cardkit.v1.card.create.assert_not_called()
        self.client.im.v1.message.create.assert_not_called()

    async def test_recall_discards_pending_text(self):
        async def chunks():
            yield "已显示"
            yield "不得显示"
            raise self.card.CardStreamStopped()
        with patch.object(self.card, "UPDATE_INTERVAL", 999):
            await self.card.sending_card("on_user", chunks())
        self.client.cardkit.v1.card_element.content.assert_not_called()
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_producer_failure_closes_stream(self):
        async def chunks():
            yield "部分结果"
            raise ValueError("producer failed")
        with self.assertRaisesRegex(ValueError, "producer failed"):
            await self.card.sending_card("on_user", chunks())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_api_failure_is_reported_and_stream_closed(self):
        self.client.cardkit.v1.card_element.content.return_value = SimpleNamespace(
            success=lambda: False, code=999, msg="denied", get_log_id=lambda: "log-1",
        )
        async def chunks():
            yield "a"
            yield "b"
        with self.assertRaisesRegex(RuntimeError, "code=999"):
            await self.card.sending_card("on_user", chunks())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_cancellation_closes_stream(self):
        async def chunks():
            yield "a"
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.card.sending_card("on_user", chunks())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_http_wait_has_a_deadline(self):
        from fersk_codex.utils.config_loader import CONFIG
        release = threading.Event()
        def blocked(request):
            release.wait(1)
            return response()
        try:
            with patch.object(self.card, "settings", return_value={"cardRequestTimeoutSeconds": 0.01}):
                with self.assertRaises(self.card.CardRequestError) as caught:
                    await self.card._call(blocked, None)
                self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        finally:
            release.set()

    async def test_reasoning_is_replaced_by_answer_in_same_card(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / "gateway.py").read_text())
        fn = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reply_content")
        async def recalled(state):
            return False
        async def events(**kwargs):
            yield {"type": "reasoning", "content": "推理一"}
            yield {"type": "reasoning", "content": "推理二"}
            # 推理片段持续累积显示。
            write = self.client.cardkit.v1.card_element.content
            self.assertEqual(write.call_args.args[0].request_body.content, "推理一推理二")
            # 模拟紧接到达的片段，被合并暂存。
            with patch.object(self.card, "UPDATE_INTERVAL", 999):
                yield {"type": "reasoning", "content": "缓冲推理"}
                yield {"type": "answer", "content": ""}
                yield {"type": "usage", "content": "token统计"}
                yield {"type": "answer", "content": "答案一"}
                # 首个非空答案绕过限频，覆盖已显示和缓冲的推理。
                self.assertEqual(write.call_args.args[0].request_body.content, "答案一")
                yield {"type": "answer", "content": "答案二"}
                yield {"type": "usage", "content": "最终统计"}
                yield {"type": "done"}
        namespace = dict(aclosing=aclosing, FerskCodex=SimpleNamespace(running=events),
                         _run_was_interrupted=recalled, CardStreamStopped=self.card.CardStreamStopped,
                         CardReplace=self.card.CardReplace,
                         CONFIG={"messages": {"codexFailure": "失败"}})
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "gateway.py", "exec"), namespace)
        stream = namespace["_reply_content"](
            SimpleNamespace(union_id="on_user"), "prompt", SimpleNamespace(run_id="run-1"),
        )
        with patch.object(self.card, "UPDATE_INTERVAL", 0):
            await self.card.sending_card("on_user", stream)
        self.client.im.v1.message.create.assert_called_once()
        self.client.cardkit.v1.card.create.assert_called_once()
        writes = self.client.cardkit.v1.card_element.content.call_args_list
        self.assertEqual([c.args[0].request_body.content for c in writes],
                         ["推理一推理二", "答案一", "答案一答案二"])
        close = self.client.cardkit.v1.card.settings.call_args.args[0].request_body
        self.assertEqual(json.loads(close.settings)["config"]["summary"]["content"], "答案一答案二")
        self.assertEqual([c.args[0].request_body.sequence for c in writes] + [close.sequence], [1, 2, 3, 4])

    async def test_gateway_adapter_streams_errors_and_stops_on_recall(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / "gateway.py").read_text())
        fn = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reply_content")
        state = SimpleNamespace(interrupted=False, run_id="run-1")
        closed = []
        async def events(**kwargs):
            try:
                yield {"type": "answer", "content": "正文"}
                yield {"type": "error", "content": "错误"}
            finally:
                closed.append(True)
        async def recalled(state):
            return state.interrupted
        namespace = dict(aclosing=aclosing, FerskCodex=SimpleNamespace(running=events),
                         _run_was_interrupted=recalled, CardStreamStopped=self.card.CardStreamStopped,
                         CardReplace=self.card.CardReplace,
                         CONFIG={"messages": {"codexFailure": "失败"}})
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "gateway.py", "exec"), namespace)
        stream = namespace["_reply_content"](SimpleNamespace(union_id="on_user"), "prompt", state)
        self.assertEqual([chunk async for chunk in stream], [self.card.CardReplace("正文"), "\n\n错误"])
        stream = namespace["_reply_content"](SimpleNamespace(union_id="on_user"), "prompt", state)
        self.assertEqual(await anext(stream), self.card.CardReplace("正文"))
        state.interrupted = True
        with self.assertRaises(self.card.CardStreamStopped):
            await anext(stream)
        self.assertEqual(len(closed), 2)

    async def test_tail_flushes_during_silence_without_cancelling_source(self):
        flushed, finish = asyncio.Event(), asyncio.Event()
        cancelled = []
        def after(operation, request):
            if operation is self.client.cardkit.v1.card_element.content:
                flushed.set()
        self.use_async_api(after)
        async def chunks():
            yield "推理"
            yield "尾部"
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        with patch.object(self.card, "UPDATE_INTERVAL", 0.02):
            task = asyncio.create_task(self.card.sending_card("on_user", chunks()))
            try:
                await asyncio.wait_for(flushed.wait(), 1)
                self.assertFalse(task.done())
                self.assertFalse(cancelled)
                write = self.client.cardkit.v1.card_element.content.call_args.args[0]
                self.assertEqual(write.request_body.content, "推理尾部")
            finally:
                finish.set()
                await asyncio.wait_for(task, 1)

    async def test_nine_minute_rotation_closes_while_silent_and_continues_on_demand(self):
        self.assertEqual(self.card.STREAM_LIFETIME, 540)
        closed, resume, second, finish = (asyncio.Event() for _ in range(4))
        def after(operation, request):
            if operation is self.client.cardkit.v1.card.settings:
                closed.set()
            if operation is self.client.im.v1.message.create and operation.call_count == 2:
                second.set()
        self.use_async_api(after)
        self.client.cardkit.v1.card.create.side_effect = [response(card_id="first"), response(card_id="second")]
        self.client.im.v1.message.create.side_effect = [response(message_id="m1"), response(message_id="m2")]
        async def chunks():
            yield "旧卡"
            yield "尾部"
            await resume.wait()
            yield "新内容"
            await finish.wait()
        with (patch.object(self.card, "STREAM_LIFETIME", .05),
              patch.object(self.card, "UPDATE_INTERVAL", 999)):
            task = asyncio.create_task(self.card.sending_card("on_user", chunks()))
            try:
                await asyncio.wait_for(closed.wait(), 1)
                self.client.im.v1.message.create.assert_called_once()
                self.assertFalse(task.done())
                write = self.client.cardkit.v1.card_element.content.call_args.args[0]
                self.assertEqual(write.card_id, "first")
                self.assertEqual(write.request_body.content, "旧卡尾部")
                resume.set()
                await asyncio.wait_for(second.wait(), 1)
            finally:
                resume.set()
                finish.set()
                result = await asyncio.wait_for(task, 1)
        self.assertEqual(result, "m2")
        bodies = [json.loads(c.args[0].request_body.data)
                  for c in self.client.cardkit.v1.card.create.call_args_list]
        self.assertEqual(bodies[1]["body"]["elements"][0]["content"], "新内容")
        self.assertEqual(bodies[1]["header"]["title"]["content"], "Codex")
        closes = self.client.cardkit.v1.card.settings.call_args_list
        self.assertEqual([c.args[0].card_id for c in closes], ["first", "second"])
        self.assertEqual([c.args[0].request_body.sequence for c in closes], [2, 1])

    async def test_expiry_without_further_text_does_not_create_empty_card(self):
        closed = asyncio.Event()
        self.use_async_api(lambda op, req: closed.set()
                           if op is self.client.cardkit.v1.card.settings else None)
        async def chunks():
            yield "只有这一段"
            await asyncio.wait_for(closed.wait(), 1)
        with patch.object(self.card, "STREAM_LIFETIME", .02):
            await asyncio.wait_for(self.card.sending_card("on_user", chunks()), 1)
        self.client.cardkit.v1.card.create.assert_called_once()
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_closed_stream_recovers_unsent_suffix_and_full_replacement(self):
        self.use_async_api()
        for chunk, expected in [("续写", "续写"), (self.card.CardReplace("旧文答案"), "旧文答案")]:
            with self.subTest(chunk=chunk):
                self.client.cardkit.v1.card.create.reset_mock()
                self.client.cardkit.v1.card.create.side_effect = [response(card_id="old"), response(card_id="new")]
                self.client.cardkit.v1.card_element.content.return_value = SimpleNamespace(
                    success=lambda: False, code=300309, msg="streaming mode is closed")
                async def chunks():
                    yield "旧文"
                    yield chunk
                await self.card.sending_card("on_user", chunks())
                body = json.loads(self.client.cardkit.v1.card.create.call_args.args[0].request_body.data)
                self.assertEqual(body["body"]["elements"][0]["content"], expected)

    async def test_delivery_failure_drains_upstream_before_reporting(self):
        self.use_async_api()
        self.client.cardkit.v1.card_element.content.return_value = SimpleNamespace(
            success=lambda: False, code=999, msg="unavailable")
        completed = []
        async def chunks():
            yield "一"
            yield "二"
            yield "三"
            completed.append(True)
        with patch.object(self.card, "UPDATE_INTERVAL", 0):
            with self.assertRaises(self.card.CardDeliveryError):
                await self.card.sending_card("on_user", chunks())
        self.assertEqual(completed, [True])
        self.client.cardkit.v1.card_element.content.assert_called_once()
        self.client.im.v1.message.create.assert_called_once()

    async def test_cancellation_cleans_pending_read_and_closes_card(self):
        waiting, cleaned = asyncio.Event(), asyncio.Event()
        self.use_async_api()
        async def chunks():
            try:
                yield "一"
                waiting.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        task = asyncio.create_task(self.card.sending_card("on_user", chunks()))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_commentary_and_tools_do_not_hide_following_reasoning(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / "gateway.py").read_text())
        fn = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_reply_content")
        async def recalled(state):
            return False
        async def events():
            yield {"type": "reasoning", "content": "推理一", "item_id": "r1"}
            yield {"type": "answer", "phase": "commentary", "content": "进度", "item_id": "a1"}
            yield {"type": "commandExecution", "content": "不应出现"}
            yield {"type": "usage", "content": "统计不应出现"}
            yield {"type": "reasoning", "content": "推理二", "item_id": "r2"}
            yield {"type": "reasoning", "content": "后续", "item_id": "r2"}
            yield {"type": "answer", "phase": "final_answer", "content": "最终答案", "item_id": "a2"}
        namespace = dict(aclosing=aclosing, _run_was_interrupted=recalled,
                         CardStreamStopped=self.card.CardStreamStopped, CardReplace=self.card.CardReplace)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "gateway.py", "exec"), namespace)
        chunks = [c async for c in namespace["_reply_content"](None, None, None, events=events())]
        self.assertEqual(chunks, ["推理一", "\n\n进度", "\n\n推理二", "后续", self.card.CardReplace("最终答案")])


class CardSteerTests(unittest.IsolatedAsyncioTestCase):
    """Control/HTTP races with the actual sender and bounded source prefetch."""

    setUp = LarkCardTests.setUp
    use_async_api = LarkCardTests.use_async_api

    async def start_controlled(self, *, cancelled=lambda: False):
        from fersk_codex.utils.config_loader import CONFIG
        queue = asyncio.Queue()
        opened = asyncio.Event()
        self.use_async_api(lambda op, req: opened.set()
                           if op is self.client.im.v1.message.create else None)
        self.client.cardkit.v1.card.create.side_effect = [
            response(card_id=f"c{i}") for i in range(1, 10)]
        session = self.card.CardStreamSession(cancelled=cancelled)
        async def source():
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield {"type": "text", "content": chunk}
        async def content():
            async with aclosing(session.events(source())) as events:
                async for event in events:
                    yield event["control"] if event["type"] == "card_control" else event["content"]
        async def send():
            async with aclosing(content()) as stream:
                return await self.card.sending_card("on_user", stream, session=session)
        task = asyncio.create_task(send())
        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        await queue.put("old")
        await asyncio.wait_for(opened.wait(), 1)
        return session, queue, task, CONFIG["messages"]["steerAccepted"]

    async def test_steer_barrier_pauses_timer_and_discards_old_buffer(self):
        with patch.object(self.card, "UPDATE_INTERVAL", .04):
            session, queue, task, text = await self.start_controlled()
            await queue.put("unsent")
            async with asyncio.timeout(1):
                while session.accumulated != "oldunsent":
                    await asyncio.sleep(0)
            async with session.steering(text) as barrier:
                await asyncio.sleep(.06)
                self.client.cardkit.v1.card_element.content.assert_not_called()
                barrier.decide(True)
                self.assertTrue(await barrier.applied)
            await queue.put("new")
            await queue.put(None)
            await asyncio.wait_for(task, 1)
        writes = self.client.cardkit.v1.card_element.content.call_args_list
        self.assertEqual([(c.args[0].card_id, c.args[0].request_body.content) for c in writes], [("c2", "new")])

    async def test_failed_steer_preserves_old_buffer(self):
        with patch.object(self.card, "UPDATE_INTERVAL", 999):
            session, queue, task, text = await self.start_controlled()
            await queue.put("tail")
            async with session.steering(text):
                pass
            await queue.put(None)
            await asyncio.wait_for(task, 1)
        self.client.cardkit.v1.card.create.assert_called_once()
        self.assertEqual(self.client.cardkit.v1.card_element.content.call_args.args[0].request_body.content, "oldtail")

    async def test_accepted_steer_with_eof_closes_placeholder_as_finished(self):
        from fersk_codex.utils.config_loader import CONFIG
        session, queue, task, text = await self.start_controlled()
        async with session.steering(text) as barrier:
            await queue.put(None)
            await asyncio.sleep(0)
            barrier.decide(True)
            self.assertTrue(await barrier.applied)
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.client.cardkit.v1.card.create.call_count, 2)
        self.assertEqual(self.client.cardkit.v1.card_element.content.call_args.args[0].request_body.content,
                         CONFIG["messages"]["steerCompleted"])

    async def test_closed_session_never_waits_for_missing_writer(self):
        session, queue, task, text = await self.start_controlled()
        await queue.put(None)
        await task
        async with asyncio.timeout(1), session.steering(text) as barrier:
            self.assertFalse(await barrier.ready)
            self.assertFalse(await barrier.applied)

    async def test_sender_cancellation_releases_pending_steer(self):
        session, queue, task, text = await self.start_controlled()
        async with session.steering(text) as barrier:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(await asyncio.wait_for(barrier.applied, 1))
        self.assertTrue(session.closed)

    async def test_steer_after_card_expired_creates_immediate_new_card(self):
        with patch.object(self.card, "STREAM_LIFETIME", .03):
            session, queue, task, text = await self.start_controlled()
            async with asyncio.timeout(1):
                while not self.client.cardkit.v1.card.settings.called:
                    await asyncio.sleep(.005)
            async with session.steering(text) as barrier:
                barrier.decide(True)
                self.assertTrue(await barrier.applied)
            self.assertEqual(self.client.cardkit.v1.card.create.call_count, 2)
            await queue.put("new")
            await queue.put(None)
            await task

    async def test_delivery_failure_still_releases_later_steer_barrier(self):
        session, queue, task, text = await self.start_controlled()
        self.client.cardkit.v1.card.create.side_effect = self.card.CardRequestError("offline")
        for _ in range(2):
            async with session.steering(text) as barrier:
                barrier.decide(True)
                self.assertFalse(await asyncio.wait_for(barrier.applied, 1))
        await queue.put(None)
        with self.assertRaises(self.card.CardDeliveryError):
            await task
        self.assertEqual(self.client.cardkit.v1.card.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
