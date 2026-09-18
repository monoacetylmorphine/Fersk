"""逐条持久化增量用量，异常退出不重复写入。"""

import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex.types import ThreadTokenUsageUpdatedNotification, TurnStatus

from fersk_codex.core import codex
from fersk_codex.utils import logging as usage_log


class TurnUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(codex.session_history, "register_session", AsyncMock()))
        self.enterContext(patch.object(codex, "_initialize_session_name", AsyncMock()))
        self.enterContext(patch.object(codex, "_sync_session_time", AsyncMock()))
        for name in ("_clients", "_processes", "_initializers", "_active_turns", "_live_turns"):
            self.enterContext(patch.object(codex.FerskCodex, name, {}))
        for name in ("_pending_interrupts", "_closed_runs"):
            self.enterContext(patch.object(codex.FerskCodex, name, set()))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = directory / "usage.sqlite"
        self.enterContext(patch.object(usage_log, "DB_PATH", self.db))
        self.enterContext(patch.object(codex, "get_user_thread", AsyncMock(return_value=None)))
        self.enterContext(patch.object(codex, "set_user_thread", AsyncMock()))
        self.enterContext(patch.object(codex, "prepare_workspace", AsyncMock()))
        self.save = self.enterContext(patch.object(codex, "SavingLog", AsyncMock(wraps=usage_log.SavingLog)))
        self.finalize = self.enterContext(patch.object(codex, "finalize_usage", AsyncMock(wraps=usage_log.finalize_usage)))
        self.factory = self.enterContext(patch.object(codex, "AsyncCodex"))

    def events(self, ending, totals=(10, 25), run_id="run-1"):
        async def stream():
            for total in totals:
                # Same SDK shape as the supplied stream reference. Thread total
                # is deliberately different from the last usage snapshot.
                last = dict(cache_write_input_tokens=0, cached_input_tokens=2,
                            input_tokens=5, output_tokens=total - 5,
                            reasoning_output_tokens=1, total_tokens=total)
                payload = ThreadTokenUsageUpdatedNotification.model_validate({
                    "threadId": "thread", "turnId": "turn",
                    "tokenUsage": {"last": last, "total": dict(last, total_tokens=1000),
                                   "modelContextWindow": 996147},
                })
                yield NS(method="thread/tokenUsage/updated", payload=payload)
            if ending == "exception":
                raise RuntimeError("stream disconnected")
            if ending == "wait":
                await asyncio.Event().wait()
            if isinstance(ending, TurnStatus):
                yield NS(method="turn/completed", payload=NS(turn=NS(
                    status=ending, error=None, duration_ms=123)))
        handle = NS(id="turn", stream=stream)
        thread = NS(id="thread", turn=AsyncMock(return_value=handle))
        self.factory.return_value.__aenter__.return_value.thread_start.return_value = thread
        return codex.FerskCodex.running("user", "hello", run_id)

    def assert_saved_all(self):
        self.finalize.assert_awaited_once()
        self.assertEqual(self.save.await_count, 2)
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT userId, threadId, total_tokens, runId FROM token_usage ORDER BY id").fetchall()
        self.assertEqual(rows, [("user", "thread", 10, "run-1"), ("user", "thread", 25, "run-1")])

    async def test_completed(self):
        events = [e async for e in self.events(TurnStatus.completed)]
        self.assertEqual(events[-1]["type"], "done")
        self.assert_saved_all()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT SUM(total_tokens), MIN(taskDuration_ms), MAX(taskDuration_ms) FROM token_usage WHERE runId = 'run-1'").fetchone(), (35, 123, 123))

    async def test_written_before_next_event(self):
        events = self.events("wait")
        await anext(events)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT total_tokens, runId FROM token_usage").fetchall(), [(10, "run-1")])
        self.finalize.assert_not_awaited()
        await events.aclose()
        self.save.assert_awaited_once()
        self.finalize.assert_awaited_once()

    async def test_no_usage_does_not_insert_zero_row(self):
        await self.drain(self.events(TurnStatus.completed, totals=()))
        self.save.assert_not_awaited()
        self.assertFalse(self.db.exists())
        self.finalize.assert_not_awaited()

    async def test_generated_run_id_is_shared_and_unique_per_run(self):
        for _ in range(2):
            await self.drain(self.events(TurnStatus.completed, run_id=None))
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT runId, COUNT(*), SUM(total_tokens) FROM token_usage GROUP BY runId").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(run and count == 2 and total == 35 for run, count, total in rows))

    async def test_failed(self):
        events = [e async for e in self.events(TurnStatus.failed)]
        self.assertEqual(events[-1]["type"], "error")
        self.assert_saved_all()

    async def test_interrupted(self):
        events = [e async for e in self.events(TurnStatus.interrupted)]
        self.assertEqual(events[-1]["type"], "interrupted")
        self.assert_saved_all()

    async def test_stream_exception(self):
        events = [e async for e in self.events("exception")]
        self.assertEqual(events[-1]["type"], "error")
        self.assert_saved_all()

    async def test_unexpected_eof(self):
        events = [e async for e in self.events("eof")]
        self.assertEqual(events[-1]["type"], "error")
        self.assert_saved_all()

    async def test_generator_close(self):
        events = self.events("wait")
        await anext(events)
        await anext(events)
        await events.aclose()
        await events.aclose()
        self.assert_saved_all()

    async def test_cancel_during_stream(self):
        events = self.events("wait")
        await anext(events)
        await anext(events)
        task = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_saved_all()

    async def test_timeout_during_stream(self):
        events = self.events("wait")
        await anext(events)
        await anext(events)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.01):
                await anext(events)
        self.assert_saved_all()

    async def test_repeated_cancel_during_save_preserves_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def save(**kwargs):
            entered.set()
            await release.wait()
            await usage_log.SavingLog(**kwargs)
        self.save.side_effect = save
        events = self.events("wait")
        closing = asyncio.create_task(anext(events))
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        self.save.assert_awaited_once()
        self.finalize.assert_awaited_once()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT total_tokens FROM token_usage").fetchall(), [(10,)])

    async def test_failed_save_does_not_hide_turn_failure_or_retry(self):
        self.save.side_effect = OSError("disk unavailable")
        with patch.object(codex.logger, "exception") as logger:
            events = [e async for e in self.events(TurnStatus.failed)]
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.save.await_count, 2)
        self.assertGreaterEqual(logger.call_count, 2)

    async def test_client_cleanup_failure_keeps_usage_record(self):
        events = self.events(TurnStatus.completed)
        self.factory.return_value.__aexit__.side_effect = RuntimeError("close failed")
        with patch.object(codex.FerskCodex, "force_close", AsyncMock(return_value=False)):
            with self.assertRaisesRegex(RuntimeError, "仍未确认进程退出"):
                await self.drain(events)
        self.assert_saved_all()

    async def test_save_has_bounded_deadline(self):
        async def stuck(**kwargs):
            await asyncio.Event().wait()
        self.save.side_effect = stuck
        with patch.dict(codex.settings(), cleanupTimeoutSeconds=0.01), patch.object(codex.logger, "exception") as logger:
            events = await asyncio.wait_for(self.drain(self.events(TurnStatus.completed)), 1)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.save.await_count, 2)
        self.assertGreaterEqual(logger.call_count, 2)

    @staticmethod
    async def drain(events):
        return [e async for e in events]
