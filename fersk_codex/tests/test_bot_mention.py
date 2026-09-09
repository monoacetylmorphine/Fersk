"""Group mention identity checks with no credentials or network access."""

from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from fersk_codex.utils.config_loader import CONFIG
import test_stop_command as helpers


class BotMentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.StopTests.setUp(self)
        self.enterContext(patch.dict(self.g.os.environ, {}, clear=True))

    def configure(self, **fields):
        self.enterContext(patch.dict(CONFIG["lark"]["credentials"], fields, clear=True))

    def test_open_id_only_survives_renaming(self):
        self.configure(robotOpenIdEnv="TEST_BOT_ID")
        self.g.os.environ["TEST_BOT_ID"] = "ou_bot"
        self.assertEqual(self.g._bot_identity(required=True), ("ou_bot", ""))
        self.assertTrue(self.g._is_bot_mentioned([NS(id=NS(open_id="ou_bot"), name="renamed")]))
        self.assertFalse(self.g._is_bot_mentioned([NS(id=NS(open_id="ou_other"))]))

    def test_name_only(self):
        self.configure(robotNameEnv="TEST_BOT_NAME")
        self.g.os.environ["TEST_BOT_NAME"] = "Bot"
        self.assertEqual(self.g._bot_identity(required=True), ("", "Bot"))
        self.assertTrue(self.g._is_bot_mentioned([NS(name="Bot")]))
        self.assertFalse(self.g._is_bot_mentioned([NS(name="Other")]))

    def test_both_use_or_matching(self):
        self.configure(robotOpenIdEnv="TEST_BOT_ID", robotNameEnv="TEST_BOT_NAME")
        self.g.os.environ.update(TEST_BOT_ID="ou_bot", TEST_BOT_NAME="Bot")
        for mention in (NS(id=NS(open_id="ou_bot"), name="Renamed"),
                        NS(id=NS(open_id="ou_other"), name="Bot")):
            self.assertTrue(self.g._is_bot_mentioned([mention]))
        self.assertFalse(self.g._is_bot_mentioned([NS(id=NS(open_id="ou_other"), name="Other")]))
        self.g.os.environ.pop("TEST_BOT_NAME")
        self.assertEqual(self.g._bot_identity(required=True), ("ou_bot", ""))

    def test_missing_empty_and_whitespace_never_match(self):
        self.configure(robotOpenIdEnv="TEST_BOT_ID", robotNameEnv="TEST_BOT_NAME")
        for value in (None, "", " \t "):
            if value is not None:
                self.g.os.environ.update(TEST_BOT_ID=value, TEST_BOT_NAME=value)
            for mentions in (None, [], [NS()], [NS(id=None, name=None)],
                             [NS(id=NS(open_id=None), name="")]):
                self.assertFalse(self.g._is_bot_mentioned(mentions))
            with self.assertRaisesRegex(RuntimeError, "至少一个非空"):
                self.g._bot_identity(required=True)

    async def test_startup_rejects_missing_identity_before_connecting(self):
        self.configure(robotOpenIdEnv="TEST_BOT_ID")
        with patch.object(self.g, "create_websocket_client") as connect:
            with self.assertRaisesRegex(RuntimeError, "至少一个非空"):
                await self.g.main()
        connect.assert_not_called()

    async def test_group_without_bot_mention_is_ignored(self):
        self.configure(robotOpenIdEnv="TEST_BOT_ID")
        self.g.os.environ["TEST_BOT_ID"] = "ou_bot"
        data = helpers.event("hello")
        data.event.message.chat_type = "group"
        data.event.message.mentions = [NS(id=NS(open_id="ou_other"))]
        self.g._route_message = AsyncMock()
        await self.g.processing(data)
        self.g._route_message.assert_not_awaited()
        data.event.message.mentions = [NS(id=NS(open_id="ou_bot"))]
        await self.g.processing(data)
        self.g._route_message.assert_awaited_once()

    def test_console_entry_runs_async_main(self):
        self.g.main = AsyncMock()
        self.g.cli()
        self.g.main.assert_awaited_once()
