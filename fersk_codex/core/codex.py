__all__ = ["FerskCodex", "LiveTurn"]

import asyncio
import random
from dataclasses import dataclass, field
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable, TypeVar
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger
from fersk_codex.utils.workspace import prepare_workspace

from openai_codex import (
    AsyncCodex,
    InvalidParamsError,
    InvalidRequestError,
    LocalImageInput,
    MethodNotFoundError,
    Sandbox,
    TextInput,
    is_retryable_error,
)
from openai_codex.types import TurnStatus

from fersk_codex.utils.logging import SavingLog, finalize_usage
from fersk_codex.core.thread_manager import get_user_thread, set_user_thread
from fersk_codex.core import session_history
from fersk_codex.core.thread_watchdog import probes, settings, should_log_event, summarize_event

logger = get_logger("Codex")

T = TypeVar("T")


@dataclass
class LiveTurn:
    thread: object
    handle: object
    model: str
    provider: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    user_id: str | None = None


def _sandbox_from_config() -> Sandbox:
    attribute = CONFIG["codex"]["sandbox"].replace("-", "_")
    try:
        return getattr(Sandbox, attribute)
    except AttributeError as exc:
        raise RuntimeError(f"不支持的 Codex sandbox: {CONFIG['codex']['sandbox']}") from exc


async def _retry_on_overload_async(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    max_attempts: int | None = None,
    initial_delay_s: float | None = None,
    max_delay_s: float | None = None,
    jitter_ratio: float | None = None,
    backoff_multiplier: float | None = None,
) -> T:
    """Async equivalent of the SDK overload retry helper."""
    retry_config = CONFIG["codex"]["retry"]
    max_attempts = retry_config["maxAttempts"] if max_attempts is None else max_attempts
    initial_delay_s = retry_config["initialDelaySeconds"] if initial_delay_s is None else initial_delay_s
    max_delay_s = retry_config["maxDelaySeconds"] if max_delay_s is None else max_delay_s
    jitter_ratio = retry_config["jitterRatio"] if jitter_ratio is None else jitter_ratio
    backoff_multiplier = retry_config["backoffMultiplier"] if backoff_multiplier is None else backoff_multiplier
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    delay = initial_delay_s
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable_error(exc):
                raise

            jitter = delay * jitter_ratio
            sleep_for = min(max_delay_s, delay) + random.uniform(-jitter, jitter)
            logger.warning(
                "Codex 瞬态错误，准备重试: operation=%s, attempt=%s/%s, error=%s",
                operation_name, attempt, max_attempts, exc,
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            delay = min(max_delay_s, delay * backoff_multiplier)

    raise RuntimeError("unreachable")


def _error_event(exc: Exception, *, operation: str) -> dict[str, str]:
    """Convert SDK failures into the generator's user-facing event format."""
    if is_retryable_error(exc):
        code = "retry_exhausted"
        content = CONFIG["messages"]["codexBusy"]
    elif isinstance(exc, (InvalidParamsError, InvalidRequestError)):
        code = "invalid_request"
        content = CONFIG["messages"]["invalidRequest"]
    elif isinstance(exc, MethodNotFoundError):
        code = "unsupported_method"
        content = CONFIG["messages"]["unsupportedMethod"]
    else:
        code = "codex_error"
        content = CONFIG["messages"]["codexFailure"]

    logger.error("Codex 操作失败: operation=%s, code=%s, error=%s", operation, code, exc)
    return {"type": "error", "code": code, "content": content}


async def _save_turn_usage(log: dict, *, duration_ms: int | None = None, finalize: bool = False) -> None:
    """在有限时间内完成单条写入或耗时回填，抵御调用方重复取消。"""
    async def save():
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if finalize:
                    await finalize_usage(log["runId"], duration_ms)
                else:
                    await SavingLog(log=log)
        except Exception:
            # A commit may already have succeeded; do not retry and duplicate it.
            logger.exception("Token usage 日志保存失败: user_id=%s, thread_id=%s",
                             log["userId"], log["threadId"])

    saving = asyncio.create_task(save())
    cancelled = False
    while not saving.done():
        try:
            await asyncio.shield(saving)
        except asyncio.CancelledError:
            cancelled = True
    saving.result()
    if cancelled:
        raise asyncio.CancelledError


async def _initialize_session_name(user_id: str, thread, prompt: str | list) -> None:
    """仅初始化已登记的新线程；旧版本线程不拿当前输入补造首次名称。"""
    record = await session_history.get_session(user_id, thread.id)
    if record is None:
        return
    if record.thread_name is None:
        record = await session_history.claim_session_name(user_id, thread.id, prompt)
    if record.updated_at is not None:
        return
    metadata = (await thread.read(include_turns=False)).thread
    if record.thread_name is not None and metadata.name != record.thread_name:
        await thread.set_name(record.thread_name)
        metadata = (await thread.read(include_turns=False)).thread
        if metadata.name != record.thread_name:
            raise RuntimeError("线程名称未成功保存")
    await session_history.update_session_time(user_id, thread.id, metadata.updated_at)


async def _sync_session_time(user_id: str, thread) -> None:
    """元数据同步失败单独记录，不把已完成的模型任务改判为执行失败。"""
    try:
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            record = await session_history.get_session(user_id, thread.id)
            if record is not None:
                # steer 命名失败后，不能用时间同步误标为命名已完成。
                if record.thread_name is not None and record.updated_at is None:
                    logger.warning("历史会话命名尚未完成: user_id=%s, thread_id=%s", user_id, thread.id)
                    return
                metadata = (await thread.read(include_turns=False)).thread
                await session_history.update_session_time(user_id, thread.id, metadata.updated_at)
    except Exception:
        logger.exception("历史会话时间同步失败: user_id=%s, thread_id=%s", user_id, thread.id)


class FerskCodex:
    _active_turns = {}
    _live_turns: dict[str, LiveTurn] = {}
    _pending_interrupts: set[str] = set()
    _turns_guard = asyncio.Lock()
    _clients: dict[str, object] = {}
    _processes: dict[str, object] = {}
    _closed_runs: set[str] = set()
    _initializers: dict[str, asyncio.Task] = {}

    @classmethod
    async def completed_status(cls, run_id):
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
            logger.debug("无法读取最终状态: run_id=%s", run_id, exc_info=True)
        return None

    @classmethod
    @asynccontextmanager
    async def _session(cls, run_id):
        manager = AsyncCodex()
        initializing = asyncio.create_task(manager.__aenter__())
        initializing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        if run_id is not None:
            cls._clients[run_id] = manager
            cls._initializers[run_id] = initializing
        try:
            # Cancellation must not lose a subprocess that start() creates late in a worker thread.
            client = await asyncio.shield(initializing)
            proc = getattr(getattr(getattr(manager, "_client", None), "_sync", None), "_proc", None)
            if run_id is not None and proc is not None:
                cls._processes[run_id] = proc
            yield client
        finally:
            try:
                async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                    await manager.__aexit__(None, None, None)
            except (Exception, asyncio.CancelledError):
                logger.exception("关闭 Codex 客户端异常: run_id=%s", run_id)
                # Keep the reference for a later /stop retry.
                if run_id is not None:
                    if not await cls.force_close(run_id):
                        raise RuntimeError("Codex 客户端关闭后仍未确认进程退出")
            else:
                if initializing.done() and cls._clients.get(run_id) is manager:
                    cls._clients.pop(run_id, None)
                proc = cls._processes.get(run_id)
                if proc is not None:
                    try:
                        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                            await asyncio.to_thread(proc.wait, timeout=1)
                        cls._processes.pop(run_id, None)
                    except Exception:
                        logger.exception("关闭后进程退出未确认: run_id=%s", run_id)
                        if not await cls.force_close(run_id):
                            raise RuntimeError("Codex 进程退出未确认")
                if initializing.done():
                    cls._initializers.pop(run_id, None)

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
            proc = getattr(getattr(getattr(client, "_client", None), "_sync", None), "_proc", None)
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
    async def steer(cls, run_id: str, prompt: str | list,
                    *, cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Check the owning server's live status; never replay uncertain input."""
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
                    await _initialize_session_name(live.user_id, live.thread, prompt)
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
            def stop_late_process(task):
                # 初始化在线程内延迟返回时仍尝试终止进程，不重新写入运行索引。
                if not task.cancelled():
                    task.exception()
                proc = getattr(getattr(getattr(manager, "_client", None), "_sync", None), "_proc", None)
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

    @staticmethod
    async def restore_session(user_id: str, thread_id: str) -> None:
        """恢复历史并替换活跃绑定；调用方必须先停止任务并持有提交锁。"""
        if await session_history.get_session(user_id, thread_id) is None:
            raise ValueError("历史会话不存在或不属于当前用户")
        if await get_user_thread(user_id) == thread_id:
            return
        async with FerskCodex._session(None) as codex:
            thread = await codex.thread_unarchive(thread_id)
            await _initialize_session_name(user_id, thread, "")
            metadata = (await thread.read(include_turns=False)).thread
        # SDK 操作与关闭成功后才写数据库；不归档原线程，不创建替代线程。
        await session_history.update_session_time(user_id, thread_id, metadata.updated_at)
        await set_user_thread(user_id, thread_id)

    @staticmethod
    async def reset_thread(user_id: str) -> None:
        """Explicit control operation; prompt text never resets a thread."""
        thread_id = await get_user_thread(user_id)
        if thread_id:
            async with FerskCodex._session(None) as codex:
                await _retry_on_overload_async(
                    lambda: codex.thread_archive(thread_id=thread_id),
                    operation_name="thread_archive",
                )
        await set_user_thread(user_id, None)

    @staticmethod
    async def running(user_id:str, prompt:str | list, run_id: str | None = None,
                      *, notify_started: bool = False):
        try:
            thread_id = await get_user_thread(user_id)
        except Exception as error:
            yield _error_event(error, operation="thread_binding_read")
            return

        if isinstance(prompt, str):
            selection = CONFIG["codex"]["models"]["text"]
            model = selection["model"]
            model_provider = selection["provider"]

        elif isinstance(prompt, list):
            # Assembly preserves multiple text fragments as a list, including
            # rich-text nodes and audio transcripts; these still use text routing.
            route = "text" if prompt and all(isinstance(item, TextInput) for item in prompt) else "multimodal"
            selection = CONFIG["codex"]["models"][route]
            model = selection["model"]
            model_provider = selection["provider"]
            image = any(
                isinstance(item, LocalImageInput)
                for item in prompt
            )

            if image:
                selection = CONFIG["codex"]["models"]["image"]
                model = selection["model"]
                model_provider = selection["provider"]

        else:
            raise TypeError(
                f"不支持的 prompt 类型: {type(prompt).__name__}"
            )

        workspace = Path(CONFIG["storage"]["workspaceRoot"]).expanduser() / user_id
        try:
            # 10 秒是项目默认策略，兼容尚未填写新字段的挂载配置。
            await prepare_workspace(workspace, CONFIG["codex"].get("gitInitTimeoutSeconds", 10))
        except Exception as error:
            yield _error_event(error, operation="workspace_init")
            return

        thread_config = {
            "cwd":str(workspace),
            "sandbox":_sandbox_from_config(),
            "model":model,
            "model_provider":model_provider,
        }

        if run_id in FerskCodex._pending_interrupts:
            yield {"type": "interrupted"}
            return
        async with FerskCodex._session(run_id) as codex:
            if run_id in FerskCodex._pending_interrupts:
                yield {"type": "interrupted"}
                return
            runningTimestamp = datetime.now(timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"])))
            thread = None
            if thread_id:
                try:
                    thread = await _retry_on_overload_async(
                        lambda: codex.thread_resume(
                            thread_id=thread_id,
                            **thread_config,
                        ),
                        operation_name="thread_resume",
                    )
                except Exception as e:
                    if is_retryable_error(e) or isinstance(
                        e, (InvalidParamsError, MethodNotFoundError)
                    ):
                        yield _error_event(e, operation="thread_resume")
                        return

                    logger.warning("恢复线程失败 (%s), 将尝试从归档恢复线程", e)

                    try:
                        thread = await _retry_on_overload_async(
                            lambda: codex.thread_unarchive(thread_id),
                            operation_name="thread_unarchive",
                        )
                    except Exception as error:
                        if is_retryable_error(error) or isinstance(
                            error, (InvalidParamsError, MethodNotFoundError)
                        ):
                            yield _error_event(error, operation="thread_unarchive")
                            return
                        logger.warning("恢复线程失败 (%s), 从归档恢复线程失败, 创建新线程", error)
                        thread = None

            if thread_id is None or thread is None:
                try:
                    thread = await _retry_on_overload_async(
                        lambda: codex.thread_start(**thread_config),
                        operation_name="thread_start",
                    )
                except Exception as error:
                    yield _error_event(error, operation="thread_start")
                    return
                try:
                    await session_history.register_session(user_id, thread.id, prompt)
                except Exception as error:
                    yield _error_event(error, operation="session_history_register")
                    return
            logger.info("Codex : user_id=%s, thread_id=%s, prompt=%s", user_id, thread.id, prompt)
            # 启动 turn 前先保存绑定，避免数据库失败后留下已执行的请求。
            try:
                await set_user_thread(user_id, thread.id)
            except Exception as error:
                yield _error_event(error, operation="thread_binding_write")
                return
            try:
                await _initialize_session_name(user_id, thread, prompt)
            except Exception as error:
                yield _error_event(error, operation="session_history_name")
                return
            try:
                if run_id in FerskCodex._pending_interrupts:
                    yield {"type": "interrupted"}
                    return
                handle = await _retry_on_overload_async(
                    lambda: thread.turn(input=prompt),
                    operation_name="turn_start",
                )
            except Exception as error:
                yield _error_event(error, operation="turn_start")
                return

            if run_id in FerskCodex._closed_runs:
                yield {"type": "interrupted"}
                return
            live = LiveTurn(thread, handle, model, model_provider, user_id=user_id)
            probe = probes.get(run_id)
            if probe:
                probe.thread_id = thread.id
                probe.turn_id = handle.id
                probe.last_activity = asyncio.get_running_loop().time()
                probe.stage("running")
            usage_run_id = run_id or uuid4().hex
            usage_received = False
            duration_ms = None
            completed = False
            interrupted = False
            message_phases = {}
            try:
                if run_id is not None:
                    async with FerskCodex._turns_guard:
                        FerskCodex._active_turns[run_id] = handle
                        FerskCodex._live_turns[run_id] = live
                        should_interrupt = run_id in FerskCodex._pending_interrupts
                    if should_interrupt:
                        await handle.interrupt()

                if notify_started:
                    yield {"type": "started", "thread_id": thread.id, "turn_id": handle.id}

                try:
                    async with aclosing(handle.stream()) as stream:
                        async for event in stream:
                            if should_log_event(event.method):
                                logger.info("Codex event: run_id=%s, event=%s", run_id, summarize_event(event))
                            if probe:
                                probe.activity(event)

                            if event.method in {"item/started", "item/completed"}:
                                item = event.payload.item.root
                                if item.type == "agentMessage":
                                    phase = getattr(item, "phase", None)
                                    if event.method == "item/started":
                                        message_phases[item.id] = getattr(phase, "value", phase)
                                    else:
                                        message_phases.pop(item.id, None)

                            elif event.method in {
                                "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
                            }:
                                yield {"type": "reasoning", "content": event.payload.delta,
                                       "item_id": event.payload.item_id}

                            elif event.method == "item/agentMessage/delta":
                                yield {"type": "answer", "content": event.payload.delta,
                                       "item_id": event.payload.item_id,
                                       "phase": message_phases.get(event.payload.item_id)}

                            elif event.method == "thread/tokenUsage/updated":
                                if hasattr(event.payload.token_usage.last, "model_dump"):
                                    usage = event.payload.token_usage.last.model_dump()
                                    usage_received = True
                                    await _save_turn_usage({
                                        "timeStamp": datetime.now(runningTimestamp.tzinfo).strftime(
                                            CONFIG["logging"]["timestampFormat"]),
                                        "userId": user_id,
                                        "threadId": thread.id,
                                        "runId": usage_run_id,
                                        "model": model,
                                        # 收到完成事件后回填；0 表示尚未获得任务总耗时。
                                        "taskDuration_ms": 0,
                                        **usage,
                                    })
                                    yield {"type": "usage", "content": str(usage)}

                            elif event.method == "turn/completed":
                                completed = True
                                duration_ms = event.payload.turn.duration_ms
                                turn = event.payload.turn
                                if turn.status == TurnStatus.failed:
                                    message = (
                                        turn.error.message
                                        if turn.error is not None
                                        else "turn failed"
                                    )
                                    yield _error_event(
                                        RuntimeError(message),
                                        operation="turn_stream",
                                    )
                                    return
                                interrupted = turn.status == TurnStatus.interrupted
                                break
                    if not completed:
                        if probe:
                            probe.finish("failed")
                        yield _error_event(RuntimeError("事件流结束但未收到 turn/completed"),
                                           operation="turn_stream")
                        return
                except Exception as error:
                    # A started/streaming turn must not be submitted again: it
                    # may already have produced output or external side effects.
                    yield _error_event(error, operation="turn_stream")
                    return
            finally:
                # Keep the process alive until an in-flight status/steer RPC
                # finishes, even when turn/completed arrives concurrently.
                try:
                    async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]), live.lock:
                        live.closed = True
                        if run_id is not None:
                            async with FerskCodex._turns_guard:
                                FerskCodex._live_turns.pop(run_id, None)
                                FerskCodex._active_turns.pop(run_id, None)
                                FerskCodex._pending_interrupts.discard(run_id)
                    if completed:
                        await _sync_session_time(user_id, thread)
                finally:
                    # 所有退出路径仅收尾一次；已知耗时时回填，不重复插入用量。
                    if usage_received:
                        await _save_turn_usage({
                            "userId": user_id,
                            "threadId": thread.id,
                            "runId": usage_run_id,
                        }, duration_ms=duration_ms, finalize=True)

            yield {"type": "interrupted" if interrupted else "done"}
