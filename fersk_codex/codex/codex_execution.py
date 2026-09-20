"""模型路由、重试、事件流转换以及用量记录。"""

import asyncio
import random
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, TypeVar
from uuid import uuid4

from openai_codex import (
    InvalidParamsError, InvalidRequestError, LocalImageInput,
    MethodNotFoundError, Sandbox, TextInput, is_retryable_error,
)
from openai_codex.types import TurnStatus

from fersk_codex.utils.workspace import prepare_workspace
from fersk_codex.utils.logging import SavingLog, finalize_usage
from . import codex_session, session_history, thread_manager
from .codex_runtime import CodexRuntime, LiveTurn
from .codex_session import CodexSession
from .thread_watchdog import probes, should_log_event, summarize_event
from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger
from .thread_watchdog import settings

__all__ = ["FerskCodex", "LiveTurn"]

logger = get_logger("Codex")


T = TypeVar("T")


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


class FerskCodex(CodexSession, CodexRuntime):
    """执行请求，并复用会话管理和运行控制的共享状态。"""

    @classmethod
    async def running(cls, user_id:str, prompt:str | list, run_id: str | None = None,
                      *, notify_started: bool = False):
        try:
            thread_id = await thread_manager.get_user_thread(user_id)
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

        if run_id in cls._pending_interrupts:
            yield {"type": "interrupted"}
            return
        async with cls._session(run_id) as codex:
            if run_id in cls._pending_interrupts:
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
                await thread_manager.set_user_thread(user_id, thread.id)
            except Exception as error:
                yield _error_event(error, operation="thread_binding_write")
                return
            try:
                await codex_session._initialize_session_name(user_id, thread, prompt)
            except Exception as error:
                yield _error_event(error, operation="session_history_name")
                return
            try:
                if run_id in cls._pending_interrupts:
                    yield {"type": "interrupted"}
                    return
                handle = await _retry_on_overload_async(
                    lambda: thread.turn(input=prompt),
                    operation_name="turn_start",
                )
            except Exception as error:
                yield _error_event(error, operation="turn_start")
                return

            if run_id in cls._closed_runs:
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
                    async with cls._turns_guard:
                        cls._active_turns[run_id] = handle
                        cls._live_turns[run_id] = live
                        should_interrupt = run_id in cls._pending_interrupts
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
                            async with cls._turns_guard:
                                cls._live_turns.pop(run_id, None)
                                cls._active_turns.pop(run_id, None)
                                cls._pending_interrupts.discard(run_id)
                    if completed:
                        await codex_session._sync_session_time(user_id, thread)
                finally:
                    # 所有退出路径仅收尾一次；已知耗时时回填，不重复插入用量。
                    if usage_received:
                        await _save_turn_usage({
                            "userId": user_id,
                            "threadId": thread.id,
                            "runId": usage_run_id,
                        }, duration_ms=duration_ms, finalize=True)

            yield {"type": "interrupted" if interrupted else "done"}
