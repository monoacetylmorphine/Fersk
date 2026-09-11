"""验证生命周期边界，不等待真实的 24 小时。"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from fersk_codex.middleware.session_cache import ActiveCodexRun, SessionCache, RETENTION_SECONDS
import test_stop_command as helpers


class SessionCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_idle_cleanup_preserves_new_waiter_and_deduplication(self):
        cache = SessionCache()
        lock = cache.codex_locks['chat'] = asyncio.Lock()
        cache.chat_generations['chat'] = 7
        cache.remember(cache.received_message_ids, 'chat', 'old')
        with cache.hold('chat'):
            cache.release_idle('chat')
            self.assertIs(cache.codex_locks['chat'], lock)
            self.assertEqual(cache.chat_generations['chat'], 7)
        self.assertFalse(cache.codex_locks)
        self.assertFalse(cache.chat_generations)
        self.assertIn('old', cache.received_message_ids['chat'])

    def test_old_owner_cannot_release_steered_message_or_new_run(self):
        cache = SessionCache()
        old = ActiveCodexRun('old', 'chat', frozenset({'m'}))
        new = ActiveCodexRun('new', 'chat', frozenset({'m'}))
        cache.active_runs_by_chat['chat'] = new
        cache.active_runs_by_message_id['m'] = new
        cache.received_at['m'] = 123
        cache.expire_run(old)
        self.assertIs(cache.active_runs_by_chat['chat'], new)
        self.assertIs(cache.active_runs_by_message_id['m'], new)
        self.assertEqual(cache.received_at['m'], 123)
        cache.expire_run(new)
        self.assertFalse(cache.received_at)

    def test_duplicate_does_not_extend_retention(self):
        cache = SessionCache()
        with patch('fersk_codex.middleware.session_cache.time.monotonic', return_value=100):
            cache.remember(cache.received_message_ids, 'chat', 'm')
            cache.remember(cache.processed_message_ids, 'chat', 'm')
            cache.recall('m')
            cache.track_reaction('chat', 'm')
        cache.reaction_message_ids['chat'] = {'m': 'r'}
        with patch('fersk_codex.middleware.session_cache.time.monotonic', return_value=200):
            cache.remember(cache.received_message_ids, 'chat', 'm')
        cache.prune(100 + RETENTION_SECONDS - 1)
        self.assertIn('m', cache.received_message_ids['chat'])
        cache.prune(100 + RETENTION_SECONDS)
        self.assertFalse(cache.received_message_ids)
        self.assertFalse(cache.processed_message_ids)
        self.assertFalse(cache.recalled_message_ids)
        self.assertFalse(cache.reaction_message_ids)

    async def test_expired_buffer_is_cancelled_without_new_events(self):
        cache = SessionCache()
        cache.buffered_events['chat-1'] = helpers.event('image')
        task = cache.buffer_tasks['chat-1'] = asyncio.create_task(asyncio.sleep(100))
        cache._buffer_times['chat-1'] = 100
        cache.prune(100 + RETENTION_SECONDS)
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(cache.buffered_events)
        self.assertFalse(cache.buffer_tasks)


class GatewayCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.StopTests.setUp(self)

    async def test_finished_request_releases_runtime_but_rejects_duplicate(self):
        await self.g.processing(helpers.event('hello', message_id='next'))
        count = self.g.assemble_codex_input.await_count
        self.assertEqual(count, 1)
        for name in ('all_runs', 'codex_locks', 'chat_generations', 'received_at',
                     'reaction_message_ids', 'active_runs_by_chat', 'active_runs_by_message_id'):
            self.assertFalse(getattr(self.g, name), name)
        await self.g.processing(helpers.event('hello', message_id='next'))
        self.assertEqual(self.g.assemble_codex_input.await_count, count)

    async def test_history_failure_and_card_failure_release_request(self):
        self.g.getting_chat_history.side_effect = OSError('offline')
        self.g.sending_card.side_effect = OSError('offline')
        await self.g.processing(helpers.event('hello'))
        for name in ('all_runs', 'codex_locks', 'received_at', 'pending_chat_requests'):
            self.assertFalse(getattr(self.g, name), name)

    async def test_unconfirmed_stop_expires_even_when_close_fails(self):
        state = helpers.StopTests.state(self)
        state.created_at = time.monotonic() - RETENTION_SECONDS
        self.g.blocked_chats[state.chat_id] = state
        self.g.FerskCodex.discard_expired_run = AsyncMock(side_effect=OSError('unconfirmed'))
        await self.g._expire_session_cache()
        self.assertTrue(state.expired)
        self.g.FerskCodex.discard_expired_run.assert_awaited_once_with(state.run_id)
        self.assertFalse(self.g.blocked_chats)
        self.assertFalse(self.g.active_runs_by_message_id)

    async def test_history_page_size_uses_shared_setting(self):
        with patch.dict(self.g.CONFIG['messaging'], historyPageSize=3):
            await self.g.processing(helpers.event('hello'))
        self.g.getting_chat_history.assert_awaited_once_with(chat_id='chat-1', messages_num=3)
