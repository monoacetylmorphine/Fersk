"""用临时 SQLite 文件验证绑定持久化，不访问实际 state.db 或 Codex 服务。"""

from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fersk_codex.codex import codex_execution, codex_runtime, thread_manager
from fersk_codex.session import session_history
from fersk_codex.codex import codex_execution as codex, thread_manager as thread_management


class ThreadManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "nested" / "state.db"
        patcher = patch.object(thread_management, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.enterContext(patch.object(session_history, "DB_PATH", self.db_path))

    async def test_missing_user_creates_database_and_table(self) -> None:
        self.assertIsNone(await thread_management.get_user_thread("missing"))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM user_thread").fetchall(), [])

    async def test_binding_update_and_reset_survive_new_connections(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        self.assertEqual(await thread_management.get_user_thread("user-1"), "thread-1")
        await thread_management.set_user_thread("user-1", "thread-2")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM user_thread").fetchall(),
                             [("user-1", "thread-2")])
        await thread_management.set_user_thread("user-1", None)
        self.assertIsNone(await thread_management.get_user_thread("user-1"))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM user_thread").fetchall(),
                             [("user-1", None)])

    async def test_parallel_users_and_parameterized_values(self) -> None:
        users = [f"user-{index}" for index in range(10)] + ["'; DROP TABLE user_thread; --"]
        await asyncio.gather(*(
            thread_management.set_user_thread(user, f"thread:{user}") for user in users
        ))
        for user in users:
            self.assertEqual(await thread_management.get_user_thread(user), f"thread:{user}")

    async def test_database_failure_is_not_silently_ignored(self) -> None:
        self.db_path.parent.mkdir(parents=True)
        self.db_path.mkdir()
        with self.assertRaises(sqlite3.OperationalError):
            await thread_management.set_user_thread("user-1", "thread-1")

    async def test_new_command_archives_and_persists_reset(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        with patch.object(codex_runtime, "AsyncCodex") as client_class:
            client = client_class.return_value.__aenter__.return_value
            await codex.FerskCodex.reset_thread("user-1")
        client.thread_archive.assert_awaited_once_with(thread_id="thread-1")
        self.assertIsNone(await thread_management.get_user_thread("user-1"))

    async def test_archive_failure_preserves_binding(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        with patch.object(codex_runtime, "AsyncCodex") as client_class:
            client = client_class.return_value.__aenter__.return_value
            client.thread_archive.side_effect = RuntimeError("archive failed")
            with self.assertRaises(RuntimeError):
                await codex.FerskCodex.reset_thread("user-1")
        self.assertEqual(await thread_management.get_user_thread("user-1"), "thread-1")

    async def test_reset_database_failure_propagates(self) -> None:
        with patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)), patch.object(
            thread_manager, "set_user_thread", AsyncMock(side_effect=sqlite3.OperationalError("locked"))
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await codex.FerskCodex.reset_thread("user-1")

    async def test_prompt_new_is_not_a_control_command(self) -> None:
        self.enterContext(patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)))
        with patch.object(codex_runtime, "AsyncCodex") as client_class, patch.object(codex_execution, "prepare_workspace", AsyncMock()), patch.object(
            thread_manager, "set_user_thread", AsyncMock(side_effect=sqlite3.OperationalError("locked"))
        ):
            client = client_class.return_value.__aenter__.return_value
            client.thread_start.return_value.id = "new-thread"
            events = [evt async for evt in codex.FerskCodex.running("user-1", "/new")]
            client.thread_archive.assert_not_awaited()
            client.thread_start.assert_awaited_once()
            self.assertEqual(events[0]["type"], "error")

    async def test_binding_failure_prevents_turn_start(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        with (
            patch.object(codex_runtime, "AsyncCodex") as client_class,
            patch.object(codex_execution, "prepare_workspace", AsyncMock()),
            patch.object(thread_manager, "set_user_thread", new=AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"),
            )),
        ):
            client = client_class.return_value.__aenter__.return_value
            thread = client.thread_resume.return_value
            thread.id = "thread-1"
            events = [event async for event in codex.FerskCodex.running("user-1", "hello")]
        client.thread_resume.assert_awaited_once()
        thread.turn.assert_not_called()
        self.assertEqual(events[0]["type"], "error")


if __name__ == "__main__":
    unittest.main()
