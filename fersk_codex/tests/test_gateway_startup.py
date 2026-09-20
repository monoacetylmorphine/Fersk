"""离线验证网关组件组装、事件路由和启动退出，不连接外部服务。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import tomllib
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_stop_command as helpers


class GatewayStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helpers.StopTests.setUp(self)

    async def test_instances_share_one_cache_without_cross_gateway_state(self):
        first = self.runtime, self.execution, self.commands, self.router
        second = self.g.create_gateway()
        for runtime, execution, commands, router in (first, second):
            self.assertIs(execution.runtime, runtime)
            self.assertIs(commands.runtime, runtime)
            self.assertIs(execution.cache, runtime.cache)
            self.assertIs(commands.cache, runtime.cache)
            self.assertIs(router.cache, runtime.cache)
            with patch.object(router, '_cancel_buffer', AsyncMock()) as cancel:
                await commands.cancel_buffer('chat')
            cancel.assert_awaited_once_with('chat')
        self.runtime.cache.blocked_chats['chat'] = object()
        self.commands.history_cards.create('user', 'chat', [])
        self.assertFalse(second[0].cache.blocked_chats)
        self.assertFalse(second[2].history_cards.cards)
        self.assertIsNot(self.runtime.cache.codex_locks_guard, second[0].cache.codex_locks_guard)

    async def test_main_routes_events_and_cancels_maintenance_on_disconnect(self):
        builder = Mock()
        callbacks = {}
        def registration(name):
            def register(callback):
                callbacks[name] = callback
                return builder
            return register
        for name in ('im_message_receive_v1', 'card_action_trigger', 'im_message_recalled_v1',
                     'im_message_message_read_v1', 'im_message_reaction_created_v1',
                     'im_message_reaction_deleted_v1', 'im_chat_access_event_bot_p2p_chat_entered_v1'):
            getattr(builder, 'register_p2_' + name).side_effect = registration(name)
        dispatcher = NS(submit=Mock(return_value=True))
        notices = NS(submit=Mock(return_value=True))
        started, cancelled = [], []
        async def maintain(operation):
            started.append(operation)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(operation)
        state = NS(interrupted=False, probe=NS(stop_reason=None))
        self.runtime.cache.all_runs['active'] = state
        self.runtime.codex.cleanup_control_sessions = AsyncMock()
        websocket = NS(start=Mock())
        async def run_websocket(start):
            start()
            await asyncio.sleep(0)
            for content in ('hello', '/stop', '/new', '/history'):
                callbacks['im_message_receive_v1'](helpers.event(content))
            recalled = NS(event=NS())
            callbacks['im_message_recalled_v1'](recalled)
            self.assertIs(callbacks['card_action_trigger'](recalled), card_result)
            dispatcher.submit.return_value = False
            callbacks['im_message_receive_v1'](helpers.event('overflow'))
        card_result = object()
        with (patch.object(self.g, '_bot_identity') as identity,
              patch.object(self.g, 'create_gateway', return_value=(self.runtime, self.execution, self.commands, self.router)),
              patch.object(self.g, 'EventDispatcher', side_effect=[dispatcher, notices]),
              patch.object(self.g.lark.EventDispatcherHandler, 'builder', return_value=builder),
              patch.object(self.g, 'create_websocket_client', return_value=websocket) as connect,
              patch.object(self.g.asyncio, 'to_thread', side_effect=run_websocket),
              patch.object(self.runtime, 'maintain', side_effect=maintain),
              patch.object(self.runtime, '_interrupt_run', AsyncMock(return_value=True)) as stop,
              patch.object(self.commands, 'dispatch_history_action', return_value=card_result) as action,
              patch.object(self.g.journal, 'flush', AsyncMock()) as flush):
            await self.g.main()
        identity.assert_called_once_with(required=True)
        connect.assert_called_once_with(builder.build.return_value)
        websocket.start.assert_called_once()
        self.assertEqual(len(started), 4)
        self.assertCountEqual(started, cancelled)
        self.assertEqual([c.kwargs['control'] for c in dispatcher.submit.call_args_list],
                         [False, True, True, True, True, False])
        self.assertEqual(dispatcher.submit.call_args_list[4].args[0], self.commands.processing_recall)
        action.assert_called_once()
        notices.submit.assert_called_once()
        self.assertTrue(state.interrupted)
        self.assertEqual(state.probe.stop_reason, 'shutdown')
        stop.assert_awaited_once_with(state)
        flush.assert_awaited_once()

    async def test_history_cards_are_pruned_by_commands_maintenance(self):
        card = self.commands.history_cards.create('user', 'chat', [])
        from fersk_codex.services.lark.lark_interactive_card import CARD_TTL_SECONDS
        card.created_at -= CARD_TTL_SECONDS
        await self.commands.prune_history()
        self.assertFalse(self.commands.history_cards.cards)

    def test_console_and_docker_point_to_existing_main_module(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / 'pyproject.toml').read_text())
        self.assertEqual(project['project']['scripts']['fersk-codex'], 'fersk_codex.main:cli')
        self.assertIn('CMD ["python", "-m", "fersk_codex.main"]', (root / 'Dockerfile').read_text())
        self.assertTrue((root / 'main.py').is_file())
        self.assertFalse((root / 'gateway.py').exists())
