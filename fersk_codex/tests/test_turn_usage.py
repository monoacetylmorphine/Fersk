"""Persist received usage on abnormal turn exits without replaying writes."""

import asyncio
import csv
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
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = directory / "usage.sqlite"
        self.csv = directory / "usage.csv"
        self.enterContext(patch.object(usage_log, "DB_PATH", self.db))
        self.enterContext(patch.object(usage_log, "CSV_PATH", self.csv))
        self.enterContext(patch.object(codex, "get_user_thread", AsyncMock(return_value=None)))
        self.enterContext(patch.object(codex, "set_user_thread", AsyncMock()))
        self.enterContext(patch.object(codex.Path, "mkdir"))
        self.save = self.enterContext(patch.object(codex, "SavingLog", AsyncMock(wraps=usage_log.SavingLog)))
        self.factory = self.enterContext(patch.object(codex, "AsyncCodex"))

    def events(self, ending):
        async def stream():
            for total in (10, 25):
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
        return codex.FerskCodex.running("user", "hello")

    def assert_saved_once(self):
        self.save.assert_awaited_once()
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT userId, threadId, total_tokens FROM token_usage").fetchall()
        self.assertEqual(rows, [("user", "thread", 25)])
        with self.csv.open(newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], "25")

    async def test_completed(self):
        events = [e async for e in self.events(TurnStatus.completed)]
        self.assertEqual(events[-1]["type"], "done")
        self.assert_saved_once()

    async def test_failed(self):
        events = [e async for e in self.events(TurnStatus.failed)]
        self.assertEqual(events[-1]["type"], "error")
        self.assert_saved_once()

    async def test_interrupted(self):
        events = [e async for e in self.events(TurnStatus.interrupted)]
        self.assertEqual(events[-1]["type"], "interrupted")
        self.assert_saved_once()

    async def test_stream_exception(self):
        events = [e async for e in self.events("exception")]
        self.assertEqual(events[-1]["type"], "error")
        self.assert_saved_once()

    async def test_unexpected_eof(self):
        events = [e async for e in self.events("eof")]
        self.assertEqual(events[-1]["type"], "error")
        self.assert_saved_once()

    async def test_generator_close(self):
        events = self.events("wait")
        await anext(events)
        await anext(events)
        await events.aclose()
        await events.aclose()
        self.assert_saved_once()

    async def test_cancel_during_stream(self):
        events = self.events("wait")
        await anext(events)
        await anext(events)
        task = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_saved_once()

    async def test_timeout_during_stream(self):
        events = self.events("wait")
        await anext(events)
        await anext(events)
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.01):
                await anext(events)
        self.assert_saved_once()

    async def test_repeated_cancel_during_save_preserves_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def save(**kwargs):
            entered.set()
            await release.wait()
            await usage_log.SavingLog(**kwargs)
        self.save.side_effect = save
        events = self.events("wait")
        await anext(events)
        await anext(events)
        closing = asyncio.create_task(events.aclose())
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        self.assert_saved_once()

    async def test_failed_save_does_not_hide_turn_failure_or_retry(self):
        self.save.side_effect = OSError("disk unavailable")
        with patch.object(codex.logger, "exception") as logger:
            events = [e async for e in self.events(TurnStatus.failed)]
        self.assertEqual(events[-1]["type"], "error")
        self.save.assert_awaited_once()
        logger.assert_called_once()

    async def test_client_cleanup_failure_keeps_usage_record(self):
        events = self.events(TurnStatus.completed)
        self.factory.return_value.__aexit__.side_effect = RuntimeError("close failed")
        with patch.object(codex.FerskCodex, "force_close", AsyncMock(return_value=False)):
            result = await self.drain(events)
        self.assertEqual(result[-1]["type"], "done")
        self.assert_saved_once()

    async def test_save_has_bounded_deadline(self):
        async def stuck(**kwargs):
            await asyncio.Event().wait()
        self.save.side_effect = stuck
        with patch.dict(codex.settings(), cleanupTimeoutSeconds=0.01), patch.object(codex.logger, "exception") as logger:
            events = await asyncio.wait_for(self.drain(self.events(TurnStatus.completed)), 1)
        self.assertEqual(events[-1]["type"], "done")
        self.save.assert_awaited_once()
        logger.assert_called_once()

    @staticmethod
    async def drain(events):
        return [e async for e in events]
