"""Offline deadline, stop confirmation, process shutdown and journal tests."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fersk_codex.core.codex import FerskCodex, LiveTurn
from fersk_codex.core.thread_watchdog import (
    RunProbe, RunJournal, settings, should_log_event, summarize_event,
)
from fersk_codex.utils.config_loader import CONFIG, _load_config
import test_stop_command as helpers


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("fersk_codex.core.thread_watchdog.journal.record"))

    def test_hard_deadline_survives_activity_tools_and_steer(self):
        probe = RunProbe("run", "chat", frozenset({"m"}), received_at=0)
        probe.phase = "running"
        probe.last_activity = 899
        probe.tools.add("long-tool")
        probe.message_ids |= {"steered-message"}
        self.assertEqual(probe.expired(900), "run_timeout")

    def test_stream_activity_stays_live_but_only_final_item_is_recorded(self):
        probe = RunProbe("run", "chat", frozenset({"m"}), last_activity=0)
        item_data = {"id": "tool", "type": "commandExecution", "aggregatedOutput": "完成\n"}
        item = NS(id="tool", type="commandExecution", model_dump=Mock(return_value=item_data))
        with patch("fersk_codex.core.thread_watchdog.journal.record") as record:
            probe.activity(NS(method="item/started", payload=NS(item=NS(root=item))))
            self.assertEqual(probe.tools, {"tool"})
            for method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
                           "item/agentMessage/delta", "item/commandExecution/outputDelta"):
                self.assertFalse(should_log_event(method))
                probe.activity(NS(method=method))
            record.assert_not_called()
            self.assertGreater(probe.last_activity, 0)
            self.assertEqual(probe.event_count, 5)
            self.assertIsNone(probe.last_event)
            probe.activity(NS(method="item/completed", payload=NS(item=NS(root=item))))
            self.assertFalse(probe.tools)
            self.assertTrue(should_log_event("item/completed"))
            record.assert_called_once()
            entry = record.call_args.args[0]
            self.assertEqual(entry["event"], "item/completed")
            self.assertEqual(entry["lastEvent"], "item/completed")
            self.assertEqual(entry["item"], {"id": "tool", "type": "commandExecution"})
            item.model_dump.assert_not_called()

    def test_large_payloads_are_absent_from_console_and_journal_summaries(self):
        content = "PRIVATE_PAYLOAD" * 100000
        items = [
            NS(id="cmd", type="commandExecution", status="completed", exit_code=0,
               aggregated_output=content, command=content),
            NS(id="img", type="imageGeneration", status="completed", result=content,
               revised_prompt=content, saved_path=NS(root="/tmp/image.png"),
               transparent_background=False),
            NS(id="msg", type="agentMessage", text=content, phase="commentary"),
            NS(id="tool", type="mcpToolCall", result=content),
        ]
        probe = RunProbe("run", "chat", frozenset())
        for item in items:
            event = NS(method="item/completed", payload=NS(item=NS(root=item)))
            with patch("fersk_codex.core.thread_watchdog.journal.record") as record:
                probe.activity(event)
                summary = summarize_event(event)
                self.assertEqual(record.call_args.args[0]["item"], summary["item"])
                self.assertNotIn("PRIVATE_PAYLOAD", json.dumps(summary))
                self.assertLess(len(json.dumps(summary)), 500)
        self.assertEqual(items[1].result, content)
        self.assertEqual(summarize_event(NS(method="item/completed",
                         payload=NS(item=NS(root=items[1]))))["item"]["savedPath"], "/tmp/image.png")

    def test_turn_summary_omits_nested_items_and_preserves_retry_error(self):
        error = NS(message="Reconnecting... 2/5", additional_details="request timed out")
        event = NS(method="error", payload=NS(error=error, will_retry=True))
        summary = summarize_event(event)
        self.assertTrue(summary["willRetry"])
        self.assertEqual(summary["error"]["message"], error.message)
        turn = NS(id="turn", status="failed", duration_ms=42, error=error,
                  items=[NS(result="PRIVATE_PAYLOAD")])
        summary = summarize_event(NS(method="turn/completed", payload=NS(turn=turn)))
        self.assertNotIn("PRIVATE_PAYLOAD", json.dumps(summary))
        self.assertEqual(summary["turn"]["status"], "failed")
        error.additional_details = "x" * 10000
        self.assertEqual(len(summarize_event(event)["error"]["additionalDetails"]), 501)

    def test_startup_idle_and_terminal_rules(self):
        with patch.dict(settings(), maxRunSeconds=1000, startupTimeoutSeconds=10, idleTimeoutSeconds=5):
            probe = RunProbe("r", "c", frozenset(), received_at=0, phase_at=0)
            probe.phase = "starting"
            self.assertEqual(probe.expired(10), "startup_timeout")
            probe.phase = "running"
            probe.last_activity = 0
            self.assertEqual(probe.expired(5), "idle_timeout")
            probe.tools.add("tool")
            self.assertIsNone(probe.expired(5))
            probe.finish("completed")
            probe.finish("run_timeout")
            self.assertEqual(probe.terminal, "completed")
            self.assertIsNone(probe.expired(2000))

    def test_schema_and_invalid_deadlines(self):
        import jsonschema
        jsonschema.validate(CONFIG, json.loads((Path(__file__).resolve().parents[1] / "configs/config_schema.json").read_text()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (0, -1, 1.5, True, "10", float("inf")):
                config = json.loads(json.dumps(CONFIG))
                config["codex"]["watchdog"]["maxRunSeconds"] = value
                path.write_text(json.dumps(config))
                with self.assertRaises(RuntimeError):
                    _load_config(path)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ("_active_turns", "_live_turns", "_clients", "_processes", "_initializers"):
            self.enterContext(patch.object(FerskCodex, name, {}))
        self.enterContext(patch.object(FerskCodex, "_pending_interrupts", set()))
        self.enterContext(patch.object(FerskCodex, "_closed_runs", set()))
        self.enterContext(patch.dict(settings(), interruptGraceSeconds=0.03, cleanupTimeoutSeconds=0.3))

    async def test_expiry_discards_indexes_after_failed_close_and_handles_late_init(self):
        release = asyncio.Event()
        proc = NS(poll=lambda: None, kill=Mock())
        manager = NS(_client=NS(_sync=NS(_proc=None)))
        async def initialize():
            await release.wait()
            manager._client._sync._proc = proc
        task = asyncio.create_task(initialize())
        FerskCodex._clients['expired'] = manager
        FerskCodex._initializers['expired'] = task
        FerskCodex._pending_interrupts.add('expired')
        FerskCodex._closed_runs.add('expired')
        with patch.object(FerskCodex, 'force_close', AsyncMock(return_value=False)):
            await FerskCodex.discard_expired_run('expired')
        for name in ('_clients', '_initializers', '_pending_interrupts', '_closed_runs'):
            self.assertNotIn('expired', getattr(FerskCodex, name))
        release.set()
        await task
        await asyncio.sleep(0)
        proc.kill.assert_called_once()

    async def test_interrupt_ack_is_not_stop_confirmation(self):
        idle = asyncio.Event()
        order = []
        async def interrupt():
            order.append("interrupt")
        async def read():
            order.append("read")
            await idle.wait()
            return NS(thread=NS(status=NS(root=NS(type="idle"))))
        live = LiveTurn(NS(id="thread", read=read), NS(interrupt=interrupt), "m", "p")
        FerskCodex._live_turns["run"] = live
        task = asyncio.create_task(FerskCodex.interrupt_and_confirm("run"))
        await asyncio.sleep(0)
        self.assertEqual(order, ["interrupt", "read"])
        self.assertFalse(task.done())
        idle.set()
        self.assertTrue(await task)

    async def test_stuck_status_and_stuck_interrupt_escalate(self):
        async def stuck():
            await asyncio.Event().wait()
        for stuck_rpc in ("read", "interrupt"):
            read = stuck if stuck_rpc == "read" else AsyncMock()
            interrupt = stuck if stuck_rpc == "interrupt" else AsyncMock()
            FerskCodex._live_turns["r"] = LiveTurn(NS(read=read), NS(interrupt=interrupt), "m", "p")
            with patch.object(FerskCodex, "force_close", AsyncMock(return_value=False)) as close:
                self.assertFalse(await FerskCodex.interrupt_and_confirm("r"))
                close.assert_awaited_once_with("r")

    async def test_real_sdk_close_terminates_only_owned_process(self):
        from openai_codex import AsyncCodex
        # Real SDK close path, but harmless local Python workers instead of Codex/API calls.
        owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.PIPE)
        client = AsyncCodex()
        client._client._sync._proc = owned
        FerskCodex._clients["r"] = client
        try:
            self.assertTrue(await FerskCodex.force_close("r"))
            self.assertIsNotNone(owned.poll())
            self.assertIsNone(other.poll())
        finally:
            for proc in (owned, other):
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
                if proc.stdin:
                    proc.stdin.close()

    async def test_close_hang_kills_captured_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        async def close():
            await asyncio.Event().wait()
        FerskCodex._clients["r"] = NS(close=close, _client=NS(_sync=NS(_proc=proc)))
        try:
            self.assertTrue(await FerskCodex.force_close("r"))
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)

    async def test_cancelled_initialization_retains_late_process_for_stop_retry(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = Mock()
        process.poll.return_value = 0
        class DelayedClient:
            def __init__(self):
                self._client = NS(_sync=NS(_proc=None))
            async def __aenter__(self):
                entered.set()
                await release.wait()
                self._client._sync._proc = process
                return self
            async def close(self):
                if self._client._sync._proc is not None:
                    self._client._sync._proc.terminate()
                    self._client._sync._proc = None
            async def __aexit__(self, *args):
                await self.close()
        async def run():
            async with FerskCodex._session("late"):
                self.fail("cancelled initialization must not submit a turn")
        with patch("fersk_codex.core.codex.AsyncCodex", DelayedClient):
            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(await FerskCodex.force_close("late"))
            release.set()
            await FerskCodex._initializers["late"]
            self.assertTrue(await FerskCodex.force_close("late"))
            process.terminate.assert_called_once()

    async def test_interrupted_and_unexpected_eof_never_emit_done(self):
        from fersk_codex.core import codex
        from openai_codex.types import TurnStatus
        for final_status in (TurnStatus.interrupted, None):
            async def events():
                if final_status is not None:
                    yield NS(method="turn/completed", payload=NS(turn=NS(
                        status=final_status, duration_ms=1)))
            handle = NS(id="turn", stream=events)
            thread = NS(id="thread", turn=AsyncMock(return_value=handle))
            with (patch.object(codex, "AsyncCodex") as factory,
                  patch.object(codex, "get_user_thread", AsyncMock(return_value=None)),
                  patch.object(codex, "set_user_thread", AsyncMock()),
                  patch.object(codex, "prepare_workspace", AsyncMock()),
                  patch.object(codex, "SavingLog", AsyncMock())):
                factory.return_value.__aenter__.return_value.thread_start.return_value = thread
                result = [event async for event in FerskCodex.running("user", "hello", "result")]
            self.assertEqual([event["type"] for event in result],
                             ["interrupted"] if final_status else ["error"])


class GatewayWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.StopTests.setUp(self)
        self.enterContext(patch.dict(settings(), maxRunSeconds=0.06, startupTimeoutSeconds=0.04,
                                     checkIntervalSeconds=0.005, cleanupTimeoutSeconds=0.1))
        self.g.FerskCodex.completed_status = AsyncMock(return_value=None)
        self.cards = []
        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                self.cards.append(content)
            else:
                try:
                    async for chunk in content:
                        pass
                except self.g.CardStreamStopped:
                    pass
        self.g.sending_card.side_effect = card

    def batch(self):
        return helpers.batch_from_chat_history(helpers.event("hello", message_id="m1"), [])

    async def test_terminal_stream_hang_releases_without_changing_model_result(self):
        self.enterContext(patch.dict(settings(), finalizationTimeoutSeconds=0.03))
        captured = []
        async def running(**kwargs):
            state = self.g.all_runs[kwargs["run_id"]]
            captured.append(state)
            yield {"type": "started"}
            state.probe.finish("completed")
            await asyncio.Event().wait()
        self.g.FerskCodex.running = running
        await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
        state = captured[0]
        self.assertEqual(state.probe.terminal, "completed")
        self.assertTrue(state.probe.cleanup_timed_out)
        self.assertTrue(state.finished.is_set())
        self.assertFalse(self.g.active_runs_by_chat)
        self.assertFalse(self.g.all_runs)
        self.g.FerskCodex.force_close.assert_awaited_once()
        self.assertNotIn(state.chat_id, self.g.blocked_chats)
        self.assertIn(CONFIG["messages"]["cleanupTimeout"], self.cards)
        async def healthy(**kwargs):
            yield {"type": "started"}
            yield {"type": "done"}
        self.g.FerskCodex.running = healthy
        following = helpers.batch_from_chat_history(helpers.event("下一条", message_id="next"), [])
        await asyncio.wait_for(self.g._handle_message_batch(following, 0), 1)
        self.assertFalse(self.g.all_runs)

    async def test_cancel_resistant_delivery_is_quarantined_and_waiters_wake(self):
        self.enterContext(patch.dict(settings(), finalizationTimeoutSeconds=0.03))
        release = asyncio.Event()
        captured = []
        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                return
            captured.append(next(iter(self.g.all_runs.values())))
            async for _ in content:
                pass
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        self.g.sending_card.side_effect = card
        worker = None
        try:
            await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
            state = captured[0]
            worker = state.task
            self.assertTrue(state.finished.is_set())
            self.assertTrue(state.detached)
            self.assertIs(self.g.blocked_chats[state.chat_id], state)
            self.assertFalse(self.g.active_runs_by_chat)
            await self.g.processing_stop(helpers.event())
            self.assertIn(state.chat_id, self.g.blocked_chats)
        finally:
            release.set()
            if worker is not None:
                await asyncio.wait_for(worker, 1)

    async def test_stop_confirmation_hang_cannot_disable_cleanup_deadline(self):
        self.enterContext(patch.dict(settings(), finalizationTimeoutSeconds=0.03))
        async def running(**kwargs):
            yield {"type": "started"}
            await asyncio.Event().wait()
        async def confirm(run_id):
            await asyncio.Event().wait()
        self.g.FerskCodex.running = running
        self.g.FerskCodex.interrupt_and_confirm.side_effect = confirm
        await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
        self.assertFalse(self.g.active_runs_by_chat)
        self.assertFalse(self.g.all_runs)

    async def test_force_close_hang_is_bounded_and_release_is_idempotent(self):
        self.enterContext(patch.dict(settings(), finalizationTimeoutSeconds=0.02, cleanupTimeoutSeconds=0.02))
        captured = []
        async def running(**kwargs):
            state = self.g.all_runs[kwargs["run_id"]]
            captured.append(state)
            yield {"type": "started"}
            state.probe.finish("completed")
            await asyncio.Event().wait()
        async def close(run_id):
            await asyncio.Event().wait()
        self.g.FerskCodex.running = running
        self.g.FerskCodex.force_close.side_effect = close
        with patch("fersk_codex.core.thread_watchdog.journal.record") as record:
            await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
            state = captured[0]
            self.g._release_run(state)
        self.assertTrue(state.finished.is_set())
        self.assertIs(self.g.blocked_chats[state.chat_id], state)
        self.assertEqual(sum(call.args[0]["event"] == "released" for call in record.call_args_list), 1)

    async def test_silent_start_and_silent_stream_are_stopped_and_released(self):
        for started in (False, True):
            self.g.processed_message_ids.clear()
            self.cards.clear()
            async def running(**kwargs):
                if started:
                    yield {"type": "started"}
                await asyncio.Event().wait()
            self.g.FerskCodex.running = running
            await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
            self.assertEqual(self.cards, [CONFIG["messages"]["taskTimeout"]])
            self.assertFalse(self.g.active_runs_by_chat)
            self.assertFalse(self.g.active_runs_by_message_id)
            self.assertFalse(self.g.all_runs)
            self.assertFalse(self.g.codex_locks)
            self.assertFalse(self.g.received_at)

    async def test_card_wait_does_not_disable_watchdog(self):
        entered = asyncio.Event()
        original = self.g.sending_card.side_effect
        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                await original(union_id, content)
            else:
                entered.set()
                await asyncio.Event().wait()
        self.g.sending_card.side_effect = card
        await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
        self.assertTrue(entered.is_set())
        self.assertEqual(self.cards, [CONFIG["messages"]["taskTimeout"]])

    async def test_stop_card_waits_for_confirmation_and_deduplicates(self):
        state = helpers.StopTests.state(self)
        release = asyncio.Event()
        async def confirm(run_id):
            await release.wait()
            return True
        self.g.FerskCodex.interrupt_and_confirm.side_effect = confirm
        first = asyncio.create_task(self.g.processing_stop(helpers.event()))
        second = asyncio.create_task(self.g.processing_stop(helpers.event(message_id="stop2")))
        await asyncio.sleep(0)
        self.assertFalse(self.cards)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.cards, [CONFIG["messages"]["stopRequested"]])
        self.g.FerskCodex.interrupt_and_confirm.assert_awaited_once_with(state.run_id)

    async def test_unconfirmed_stop_blocks_new_work_and_allows_stop_retry(self):
        state = helpers.StopTests.state(self)
        self.g.FerskCodex.interrupt_and_confirm.return_value = False
        await self.g.processing_stop(helpers.event())
        self.assertIn(state.chat_id, self.g.blocked_chats)
        await self.g._handle_message_batch(self.batch(), 1)
        self.g.assemble_codex_input.assert_not_awaited()
        self.g.FerskCodex.interrupt_and_confirm.return_value = True
        await self.g.processing_stop(helpers.event(message_id="retry"))
        self.assertFalse(self.g.blocked_chats)

    async def test_completed_run_during_card_drain_is_not_failed(self):
        self.g.FerskCodex.completed_status.return_value = "completed"
        async def card(union_id, content, *, session=None):
            if not isinstance(content, str):
                await asyncio.sleep(0.09)
        self.g.sending_card.side_effect = card
        await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
        self.g.FerskCodex.interrupt_and_confirm.assert_not_awaited()
        self.assertFalse(self.cards)

    async def test_delivery_failure_does_not_mark_model_failed_or_interrupt_it(self):
        async def card(union_id, content, *, session=None):
            async for chunk in content:
                pass
            raise self.g.CardDeliveryError("delivery unavailable")
        self.g.sending_card.side_effect = card
        with patch("fersk_codex.core.thread_watchdog.journal.record") as record:
            await asyncio.wait_for(self.g._handle_message_batch(self.batch(), 0), 1)
        self.g.FerskCodex.interrupt_and_confirm.assert_not_awaited()
        self.assertFalse(self.g.all_runs)
        records = [call.args[0] for call in record.call_args_list]
        self.assertEqual([r["terminal"] for r in records if r["event"] == "terminal"], ["completed"])
        self.assertTrue(any(r["event"] == "delivery_failed" for r in records))

    async def test_old_history_does_not_backdate_new_run(self):
        self.g.processed_message_ids["chat-1"] = {"old": None}
        self.g.received_at["old"] = 0
        batch = helpers.batch_from_chat_history(helpers.event("hello", message_id="m1"),
            [helpers.history("hello", "m1"), helpers.history("previous", "old")])
        await asyncio.wait_for(self.g._handle_message_batch(batch, 0), 1)
        self.g.FerskCodex.interrupt_and_confirm.assert_not_awaited()
        self.assertFalse(self.cards)


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_jsonl_appends_batches_and_preserves_prior_records(self):
        with tempfile.TemporaryDirectory() as directory:
            from datetime import datetime, timedelta, timezone
            day = datetime.now(timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"]))).strftime("%Y-%m-%d")
            path = Path(directory) / f"{day}_logs.jsonl"
            path.write_text(json.dumps({"event": "old"}) + "\n", encoding="utf-8")
            journal = RunJournal(directory)
            journal.record({"event": "started"})
            journal.record({"event": "stopped"})
            await journal.flush()
            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()],
                             [{"event": "old"}, {"event": "started"}, {"event": "stopped"}])
            previous = path.read_bytes()
            journal.record({"event": "下一次运行"})
            await journal.flush()
            self.assertTrue(path.read_bytes().startswith(previous))
            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()],
                             [{"event": "old"}, {"event": "started"}, {"event": "stopped"},
                              {"event": "下一次运行"}])


if __name__ == "__main__":
    unittest.main()
