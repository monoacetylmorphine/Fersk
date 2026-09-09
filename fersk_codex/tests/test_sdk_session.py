"""验证 SDK 会话直接采用用户配置。"""
import unittest
from unittest.mock import AsyncMock, patch

from fersk_codex.core import codex


class SdkSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_preserves_user_configuration(self):
        manager = AsyncMock()
        with patch.object(codex, "AsyncCodex", return_value=manager) as factory:
            async with codex.FerskCodex._session(None) as client:
                self.assertIs(client, manager.__aenter__.return_value)
            factory.assert_called_once_with()
        manager.__aexit__.assert_awaited_once()
