"""SDK 生命周期、运行状态以及停止和 steer 控制。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from openai_codex import AsyncCodex, InvalidRequestError, LocalImageInput

from fersk_codex.session import session_codex
from .thread_watchdog import probes
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger
from .thread_watchdog import settings

if TYPE_CHECKING:
    import subprocess

    from openai_codex import AsyncThread, AsyncTurnHandle, InputItem

    from .codex_execution import RunEvent

logger = get_logger("Codex")


def _sdk_process(manager: AsyncCodex | None) -> subprocess.Popen[bytes] | None:
    """集中访问 SDK 私有进程路径"""
    return getattr(getattr(getattr(manager, "_client", None), "_sync", None), "_proc", None)


@dataclass
class LiveTurn:
    thread: AsyncThread
    handle: AsyncTurnHandle
    model: str
    provider: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    user_id: str | None = None


class CodexRuntime:
    """集中保存所有运行索引，供 FerskCodex 的执行和会话方法共享。"""
    _active_turns: dict[str, AsyncTurnHandle] = {}
    _live_turns: dict[str, LiveTurn] = {}
    _pending_interrupts: set[str] = set()
    _turns_guard = asyncio.Lock()
    _clients: dict[str, AsyncCodex] = {}
    _processes: dict[str, subprocess.Popen[bytes]] = {}
    _closed_runs: set[str] = set()
    _initializers: dict[str, asyncio.Task[AsyncCodex]] = {}
    # 仅保存控制会话的失败清理，不重放归档/恢复等业务操作。
    _control_cleanup: dict[str, tuple[float, float]] = {}

    @classmethod
    async def completed_status(cls, run_id: str) -> str | None:
        """Resolve a completion buffered behind a slow card before timing out."""
        live = cls._live_turns.get(run_id)
        if live is None:
            return None
        try:
            async with asyncio.timeout(min(1, settings()["interruptGraceSeconds"])), live.lock:
                response = await live.thread.read(include_turns=True)
                for turn in response.thread.turns:
                    status = getattr(turn.status, "value", turn.status)
                    if turn.id == live.handle.id and status in {"completed", "failed", "interrupted"}:
                        return status
        except Exception:
            logger.debug("Unable to read the final status: run_id=%s", run_id, exc_info=True)
        return None

    @classmethod
    @asynccontextmanager
    async def _session(cls, run_id: str | None) -> AsyncGenerator[AsyncCodex]:
        control = run_id is None
        run_id = run_id or f"control-{uuid4().hex}"
        manager = AsyncCodex()
        initializing = asyncio.create_task(manager.__aenter__())
        initializing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        cls._clients[run_id] = manager
        cls._initializers[run_id] = initializing

        try:
            # Cancellation must not lose a subprocess that start() creates late in a worker thread.
            client = await asyncio.shield(initializing)
            proc = _sdk_process(manager)
            if proc is not None:
                cls._processes[run_id] = proc
            yield client
        finally:
            # 外层重复取消不能打断进程回收；取消完成后仍必须向调用方传播。
            cancelled = bool(asyncio.current_task().cancelling())
            closing = asyncio.create_task(cls._close_session(run_id, manager, initializing))
            try:
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:
                        break
                closing.result()
            finally:
                if control:
                    if run_id in cls._clients or run_id in cls._initializers:
                        now = time.monotonic()
                        cls._control_cleanup[run_id] = (now, now)
                    else:
                        cls._closed_runs.discard(run_id)
                if cancelled:
                    raise asyncio.CancelledError

    @classmethod
    async def _close_session(
        cls,
        run_id: str,
        manager: AsyncCodex,
        initializing: asyncio.Task[AsyncCodex],
    ) -> None:
        # close() 可能清空 SDK 中的进程引用，必须先保存。
        proc = _sdk_process(manager)
        if proc is not None:
            cls._processes[run_id] = proc
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                await manager.__aexit__(None, None, None)
                if not initializing.done():
                    raise RuntimeError("SDK initialization still processing, closing unverified")
                late_proc = _sdk_process(manager)
                if proc is None and late_proc is not None:
                    proc = late_proc
                    cls._processes[run_id] = proc
                if proc is not None:
                    await asyncio.to_thread(proc.wait, timeout=1)
                    if proc.poll() is None:
                        raise RuntimeError("SDK processes are exited, but unverified")
        except (Exception, asyncio.CancelledError):
            logger.exception("Closing Codex client Error: run_id=%s", run_id)
            if not await cls.force_close(run_id):
                raise RuntimeError("Processes exit still unverified after Codex client was closed")
        cls._clients.pop(run_id, None)
        cls._processes.pop(run_id, None)
        cls._initializers.pop(run_id, None)

    @classmethod
    async def cleanup_control_sessions(cls) -> None:
        """后台每轮清理一个控制会话, 失败保留, 24 小时后按现有策略释放。"""
        from fersk_codex.session.session_gateway import RETENTION_SECONDS
        now = time.monotonic()
        for run_id, (created, due) in list(cls._control_cleanup.items()):
            if now < due:
                continue
            try:
                if now - created >= RETENTION_SECONDS:
                    await cls.discard_expired_run(run_id)
                elif not await cls.force_close(run_id):
                    # 来源：控制会话失败清理初始重试策略，非 SDK 限制。
                    cls._control_cleanup[run_id] = (created, time.monotonic() + 30)
                    return
                cls._control_cleanup.pop(run_id, None)
                cls._closed_runs.discard(run_id)
            except Exception:
                logger.exception("控制会话后台清理失败: run_id=%s", run_id)
                if now - created >= RETENTION_SECONDS:
                    cls._control_cleanup.pop(run_id, None)
                else:
                    cls._control_cleanup[run_id] = (created, time.monotonic() + 30)
            return

    @classmethod
    async def force_close(cls, run_id: str) -> bool:
        """Close only this run's SDK process and verify exit (SDK 0.147.0).

        Capture the process before close(), which clears the SDK's reference.
        This is not a guarantee about remote tools or detached descendants.
        """
        cls._closed_runs.add(run_id)
        client = cls._clients.get(run_id)
        proc = cls._processes.get(run_id)
        initializing = cls._initializers.get(run_id)
        if client is None and proc is None:
            return run_id not in cls._active_turns and (initializing is None or initializing.done())
        if proc is None:
            proc = _sdk_process(client)
            if proc is not None:
                cls._processes[run_id] = proc
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if client is not None:
                    await client.close()
                elif proc is not None:
                    proc.terminate()
                if proc is not None:
                    await asyncio.to_thread(proc.wait, timeout=1)
            confirmed = ((proc is not None and proc.poll() is not None)
                         or (proc is None and initializing is not None and initializing.done()))
            if initializing is not None and not initializing.done():
                confirmed = False
        except Exception:
            logger.exception("强制关闭 Codex: run_id=%s", run_id)
            confirmed = False
            if proc is not None:
                try:
                    proc.kill()
                    async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                        await asyncio.to_thread(proc.wait, timeout=1)
                    confirmed = proc.poll() is not None
                except Exception:
                    logger.exception("Codex 进程退出未确认: run_id=%s", run_id)
        probe = probes.get(run_id)
        if initializing is not None and not initializing.done():
            confirmed = False
        if probe:
            probe.record("force_close", processExited=confirmed)
        if confirmed:
            cls._processes.pop(run_id, None)
            cls._clients.pop(run_id, None)
            cls._initializers.pop(run_id, None)
        return confirmed

    @classmethod
    async def interrupt_and_confirm(cls, run_id: str) -> bool:
        """Interrupt, then poll the owning thread until idle; bound every wait."""
        cls._pending_interrupts.add(run_id)
        if run_id in cls._closed_runs:
            return await cls.force_close(run_id)
        try:
            async with asyncio.timeout(settings()["interruptGraceSeconds"]):
                while True:
                    live = cls._live_turns.get(run_id)
                    if live is not None:
                        # Keep the owning client alive while checking its status.
                        async with live.lock:
                            if not live.closed:
                                await live.handle.interrupt()
                                while True:
                                    response = await live.thread.read()
                                    status = response.thread.status.root.type
                                    logger.info("停止确认: run_id=%s, thread_id=%s, status=%s",
                                                run_id, live.thread.id, status)
                                    probe = probes.get(run_id)
                                    if probe:
                                        probe.record("stop_status", threadStatus=status)
                                    if status == "idle":
                                        return True
                                    await asyncio.sleep(0.1)
                    probe = probes.get(run_id)
                    if (probe and probe.terminal and run_id not in cls._clients
                            and run_id not in cls._processes and run_id not in cls._initializers):
                        return True
                    if (run_id not in cls._clients and run_id not in cls._active_turns
                            and run_id not in cls._processes and run_id not in cls._initializers):
                        # Startup has not reached a client; pending flag prevents submission.
                        return True
                    await asyncio.sleep(0.1)
        except Exception:
            logger.exception("中断或停止确认超时/失败: run_id=%s", run_id)
        return await cls.force_close(run_id)

    @classmethod
    async def steer(
        cls,
        run_id: str,
        prompt: str | list[InputItem],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> RunEvent:
        """Check the owning server's live status; never replay uncertain input."""
        from .codex_execution import _error_event

        live = cls._live_turns.get(run_id)
        if live is None:
            return {"type": "idle"}
        async with asyncio.timeout(settings()["interruptGraceSeconds"]), live.lock:
            if cancelled():
                return {"type": "cancelled"}
            if live.closed or run_id in cls._pending_interrupts:
                return {"type": "idle"}
            try:
                response = await live.thread.read()
                if cancelled():
                    return {"type": "cancelled"}
                status = response.thread.status.root.type
                logger.info("Thread status: thread_id=%s, turn_id=%s, status=%s",
                            live.thread.id, live.handle.id, status)
                if status == "idle":
                    return {"type": "idle"}
                if status != "active":
                    raise RuntimeError(f"线程状态无法接收输入: {status}")
                if run_id in cls._pending_interrupts:
                    return {"type": "idle"}
                # Steering cannot change model/provider. Do not silently send
                # images to the configured text-only route.
                image_route = CONFIG["codex"]["models"]["image"]
                if (isinstance(prompt, list)
                        and any(isinstance(item, LocalImageInput) for item in prompt)
                        and (live.model, live.provider) !=
                        (image_route["model"], image_route["provider"])):
                    return {"type": "error", "content": CONFIG["messages"]["steerImageUnsupported"]}
                if live.user_id is not None:
                    await session_codex._initialize_session_name(live.user_id, live.thread, prompt)
                try:
                    result = await live.handle.steer(prompt)
                except InvalidRequestError as exc:
                    # These errors explicitly mean the input was NOT accepted.
                    # Other invalid requests (e.g. review/compact) must surface.
                    if (exc.message == "no active turn to steer"
                            or exc.message.startswith("expected active turn id `")):
                        response = await live.thread.read()
                        if response.thread.status.root.type == "idle":
                            return {"type": "idle"}
                    raise
                if result.turn_id != live.handle.id:
                    raise RuntimeError("steer 返回了不同的 turn ID")
                logger.info("Steer accepted: thread_id=%s, turn_id=%s", live.thread.id, result.turn_id)
                return {"type": "steered"}
            except Exception as exc:
                return _error_event(exc, operation="turn_steer")

    @classmethod
    async def interrupt(cls, run_id: str) -> bool:
        """Interrupt a running turn, or remember the request until it starts."""
        async with cls._turns_guard:
            cls._pending_interrupts.add(run_id)
            handle = cls._active_turns.get(run_id)

        if handle is None or run_id in cls._closed_runs:
            return False

        await handle.interrupt()
        return True

    @classmethod
    async def forget_run(cls, run_id: str) -> None:
        """Drop interrupt bookkeeping after the gateway run has ended."""
        async with cls._turns_guard:
            cls._active_turns.pop(run_id, None)
            cls._live_turns.pop(run_id, None)
            cls._pending_interrupts.discard(run_id)
            if run_id not in cls._clients and run_id not in cls._processes:
                cls._closed_runs.discard(run_id)

    @classmethod
    async def discard_expired_run(cls, run_id: str) -> None:
        """24 小时后有界关闭并移除引用，失败不能使缓存永久保留。"""
        manager = cls._clients.get(run_id)
        initializing = cls._initializers.get(run_id)
        if initializing is not None and not initializing.done():
            def stop_late_process(task: asyncio.Task[AsyncCodex]) -> None:
                # 初始化在线程内延迟返回时仍尝试终止进程，不重新写入运行索引。
                if not task.cancelled():
                    task.exception()
                proc = _sdk_process(manager)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            initializing.add_done_callback(stop_late_process)
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if not await cls.force_close(run_id):
                    logger.error("过期任务进程退出未确认: run_id=%s", run_id)
        finally:
            for mapping in (cls._active_turns, cls._live_turns, cls._clients,
                            cls._processes, cls._initializers):
                mapping.pop(run_id, None)
            cls._pending_interrupts.discard(run_id)
            cls._closed_runs.discard(run_id)
