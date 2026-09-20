"""飞书网关启动入口：组装职责实例、注册事件并管理后台任务。"""

import asyncio
import sys
from pathlib import Path

import lark_oapi as lark

# 支持从项目目录直接运行 main.py。
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fersk_codex.codex.codex_execution import FerskCodex
from fersk_codex.codex.thread_watchdog import journal
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger, configure_logging
from fersk_codex.utils.event_dispatcher import EventDispatcher
from fersk_codex.services.lark.lark_client import create_websocket_client
from fersk_codex.services.lark.lark_tools import getting_chat_history, adding_reaction_emoji, delete_reaction_emoji
from fersk_codex.services.lark.lark_message_card import sending_card
from fersk_codex.middleware.message_collector import is_stop_command, is_new_command, is_history_command
from fersk_codex.middleware.message_router import MessageRouter, _bot_identity
from fersk_codex.middleware.message_assemble import assemble_codex_input
from fersk_codex.session.session_gateway import SessionCache
from fersk_codex.middleware.gateway_runtime import GatewayRuntime
from fersk_codex.middleware.gateway_execution import GatewayExecution
from fersk_codex.middleware.gateway_commands import GatewayCommands

logger = get_logger("Message")


def create_gateway() -> tuple[GatewayRuntime, GatewayExecution, GatewayCommands, MessageRouter]:
    """每次创建独立网关，组件共享唯一缓存；不启动网络或后台任务。"""
    cache = SessionCache()
    runtime = GatewayRuntime(cache, codex=FerskCodex, send_card=sending_card,
                             delete_reaction=delete_reaction_emoji)
    execution = GatewayExecution(runtime, assemble_input=assemble_codex_input)
    commands = GatewayCommands(runtime, cancel_buffer=lambda chat_id: router._cancel_buffer(chat_id))
    router = MessageRouter(
        cache, submit=execution._handle_message_batch,
        stop=commands.processing_stop, new=commands.processing_new,
        notify=runtime._notify_terminal, fetch_history=getting_chat_history,
        add_reaction=adding_reaction_emoji, clear_reactions=runtime._clear_reaction,
        send_card=lambda *args: runtime.send_card(*args),
        buffer_seconds=lambda: CONFIG["messaging"]["bufferWindowSeconds"],
        history=commands.processing_history,
    )
    return runtime, execution, commands, router


async def main() -> None:
    configure_logging(CONFIG["logging"].get("logLevel", "INFO"))
    _bot_identity(required=True)
    runtime, _, commands, router = create_gateway()
    loop = asyncio.get_running_loop()
    dispatcher = EventDispatcher(loop, CONFIG["messaging"].get("maxPendingEvents", 32))
    notices = EventDispatcher(loop, capacity=1)

    async def busy(data):
        await sending_card(data.event.message.chat_id, "当前任务繁忙，请稍后重试。")

    def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        message = data.event.message
        logger.debug("收到消息: chat_id=%s, message_id=%s, type=%s",
                     message.chat_id, message.message_id, message.message_type)
        control = (is_stop_command(message.message_type, message.content)
                   or is_new_command(message.message_type, message.content)
                   or (message.chat_type == "p2p" and is_history_command(message.message_type, message.content)))
        if not dispatcher.submit(router.processing, data, control=control):
            notices.submit(busy, data)

    def do_p2_im_message_recalled_v1(data: lark.im.v1.P2ImMessageRecalledV1) -> None:
        dispatcher.submit(commands.processing_recall, data, control=True)

    def do_p2_im_message_read_v1(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
        pass

    def do_p2_im_message_reaction_created_v1(data: lark.im.v1.P2ImMessageReactionCreatedV1) -> None:
        pass

    def do_p2_im_message_reaction_deleted_v1(data: lark.im.v1.P2ImMessageReactionDeletedV1) -> None:
        pass

    def do_p2_im_chat_access_event_bot_p2p_chat_entered_v1(data: lark.im.v1.P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        pass

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .register_p2_card_action_trigger(lambda data: commands.dispatch_history_action(dispatcher, data))
        .register_p2_im_message_recalled_v1(do_p2_im_message_recalled_v1)
        .register_p2_im_message_message_read_v1(do_p2_im_message_read_v1)
        .register_p2_im_message_reaction_created_v1(do_p2_im_message_reaction_created_v1)
        .register_p2_im_message_reaction_deleted_v1(do_p2_im_message_reaction_deleted_v1)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(do_p2_im_chat_access_event_bot_p2p_chat_entered_v1)
        .build()
    )

    websocket_client = create_websocket_client(event_handler)
    cleanup_tasks = [asyncio.create_task(runtime.maintain(operation)) for operation in
                     (runtime._expire_session_cache, runtime._retry_reactions,
                      commands.prune_history, runtime.codex.cleanup_control_sessions)]
    try:
        await asyncio.to_thread(websocket_client.start)
    finally:
        for task in cleanup_tasks:
            task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        for state in list(runtime.cache.all_runs.values()):
            state.interrupted = True
            state.probe.stop_reason = state.probe.stop_reason or "shutdown"
        await asyncio.gather(*(runtime._interrupt_run(state) for state in list(runtime.cache.all_runs.values())),
                             return_exceptions=True)
        try:
            await journal.flush()
        except Exception:
            logger.exception("退出时运行日志尚未写入完成")


def cli() -> None:
    """同步命令入口，负责启动异步网关。"""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
