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


class ReactionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.StopTests.setUp(self)

    async def test_all_reactions_wait_for_card_completion_even_on_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                self.g.cache.received_message_ids.clear()
                self.g.cache.processed_message_ids.clear()
                self.g.delete_reaction_emoji.reset_mock()
                entered, finish = asyncio.Event(), asyncio.Event()
                async def send(*args, **kwargs):
                    content = kwargs.get('content', args[1] if len(args) > 1 else None)
                    if not isinstance(content, str):
                        async for _ in content:
                            pass
                        entered.set()
                        await finish.wait()
                        if failure:
                            raise self.g.CardDeliveryError('delivery failed')
                self.g.sending_card = send
                self.g.reaction_message_ids['chat-1'] = {'m1': 'r1', 'm2': 'r2'}
                batch = helpers.batch_from_chat_history(helpers.event('hello', message_id='m2'),
                    [helpers.history('hello', 'm2'), helpers.history('hello', 'm1')])
                task = asyncio.create_task(self.g._handle_message_batch(batch, 0))
                await asyncio.wait_for(entered.wait(), 1)
                self.g.delete_reaction_emoji.assert_not_awaited()
                finish.set()
                await asyncio.wait_for(task, 1)
                self.assertEqual(self.g.delete_reaction_emoji.await_count, 2)
                self.assertFalse(self.g.reaction_message_ids)
                self.assertFalse(self.g.cache.pending_reactions)

    async def test_failed_delete_survives_release_and_background_retries_without_input(self):
        self.g.delete_reaction_emoji.return_value = False
        await self.g.processing(helpers.event('hello', message_id='m1'))
        self.assertFalse(self.g.all_runs)
        self.assertFalse(self.g.codex_locks)
        key = ('chat-1', 'm1', 'reaction-1')
        pending = self.g.cache.pending_reactions[key]
        self.assertIn('m1', self.g.reaction_message_ids['chat-1'])
        attempts = self.g.delete_reaction_emoji.await_count
        await self.g._retry_reactions()
        self.assertEqual(self.g.delete_reaction_emoji.await_count, attempts)
        pending.due = 0
        self.g.delete_reaction_emoji.return_value = True
        await self.g._retry_reactions()
        self.assertFalse(self.g.cache.pending_reactions)
        self.assertFalse(self.g.reaction_message_ids)

    async def test_cancel_during_delete_keeps_all_snapshot_records(self):
        entered = asyncio.Event()
        async def stuck(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.g.delete_reaction_emoji.side_effect = stuck
        self.g.reaction_message_ids['chat-1'] = {'m1': 'r1', 'm2': 'r2'}
        task = asyncio.create_task(self.g._clear_reaction('chat-1'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.g.cache.pending_reactions), 2)
        self.assertFalse(self.g.reactions_being_cleared)

    async def test_retry_old_id_cannot_remove_new_reaction(self):
        self.g.cache.queue_reaction('chat-1', 'm1', 'old')
        self.g.reaction_message_ids['chat-1'] = {'m1': 'new'}
        await self.g._retry_reactions()
        self.assertEqual(self.g.reaction_message_ids['chat-1']['m1'], 'new')
        self.assertFalse(self.g.cache.pending_reactions)

    async def test_running_owner_is_not_cleared_by_stop_snapshot(self):
        worker = asyncio.create_task(asyncio.Event().wait())
        state = ActiveCodexRun('run', 'chat-1', frozenset({'m1'}), task=worker)
        self.g.active_runs_by_message_id['m1'] = state
        self.g.reaction_message_ids['chat-1'] = {'m1': 'r1'}
        try:
            await self.g._clear_reaction('chat-1')
            self.g.delete_reaction_emoji.assert_not_awaited()
            self.assertFalse(self.g.cache.pending_reactions)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def test_pending_deletion_expiry_and_capacity_are_bounded(self):
        from fersk_codex.utils.config_loader import CONFIG
        cache = self.g.cache
        with patch.dict(CONFIG['messaging'], recallCacheMaxEntries=1):
            cache.reaction_message_ids['chat'] = {'m1': 'r1', 'm2': 'r2'}
            cache.queue_reaction('chat', 'm1', 'r1')
            with self.assertLogs('fersk.codex', level='ERROR'):
                cache.queue_reaction('chat', 'm2', 'r2')
            self.assertEqual(len(cache.pending_reactions), 1)
            key, pending = next(iter(cache.pending_reactions.items()))
            with self.assertLogs('fersk.codex', level='ERROR'):
                cache.prune(pending.created_at + RETENTION_SECONDS + 1)
            self.assertFalse(cache.pending_reactions)
            self.assertFalse(cache.reaction_message_ids)

    async def test_stop_and_recall_wait_for_output_card_cleanup(self):
        from types import SimpleNamespace as NS
        for action in ('stop', 'recall'):
            with self.subTest(action=action):
                self.g.received_message_ids.clear()
                self.g.processed_message_ids.clear()
                self.g.delete_reaction_emoji.reset_mock()
                entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                async def send(*args, **kwargs):
                    content = kwargs.get('content', args[1] if len(args) > 1 else None)
                    if isinstance(content, str):
                        return
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        closing.set()
                        await release.wait()
                self.g.sending_card = send
                self.g.reaction_message_ids['chat-1'] = {'m1': 'r1'}
                data = helpers.event('hello', message_id='m1')
                batch = helpers.batch_from_chat_history(data, [])
                task = asyncio.create_task(self.g._handle_message_batch(batch, 0))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    if action == 'stop':
                        await self.g._stop_chat(helpers.event())
                    else:
                        await self.g.processing_recall(NS(event=NS(
                            recall_type='message_owner', message_id='m1', chat_id='chat-1')))
                    await asyncio.wait_for(closing.wait(), 1)
                    self.g.delete_reaction_emoji.assert_not_awaited()
                    release.set()
                    await asyncio.wait_for(task, 1)
                    self.g.delete_reaction_emoji.assert_awaited_once_with(message_id='m1', reaction_id='r1')
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
