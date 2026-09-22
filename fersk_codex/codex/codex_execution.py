"""模型路由、重试、事件流转换以及用量记录。"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NotRequired, TYPE_CHECKING, TypeVar, TypedDict
from uuid import uuid4

from openai_codex import (
    InvalidParamsError, InvalidRequestError, LocalImageInput,
    MethodNotFoundError, Sandbox, TextInput, is_retryable_error,
)
from openai_codex.types import TurnStatus

from fersk_codex.codex.codex_workspace import prepare_workspace, workspace_environment
from fersk_codex.utils.logging import SavingLog, finalize_usage
from . import thread_manager
from fersk_codex.session import session_codex, session_history
from .codex_runtime import CodexRuntime, LiveTurn
from fersk_codex.session.session_codex import CodexSession
from .thread_watchdog import RunProbe, probes, should_log_event, summarize_event
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger
from .thread_watchdog import settings

if TYPE_CHECKING:
    from openai_codex import InputItem

    from fersk_codex.services.lark.lark_message_card import CardSteer

T = TypeVar("T")


__all__ = ["FerskCodex", "LiveTurn"]

logger = get_logger("Codex")


class RunEvent(TypedDict):
    """SDK 与网关之间的字典事件；可选字段保持原有缺省语义。"""

    type: str
    content: NotRequired[str]
    code: NotRequired[str]
    item_id: NotRequired[str]
    thread_id: NotRequired[str]
    turn_id: NotRequired[str]
    phase: NotRequired[str | None]
    control: NotRequired[CardSteer]


def _resolve_model(prompt: str | list[InputItem]) -> tuple[str, str]:
    """保留文本、附件与图片路由优先级，空列表沿用多模态路由。"""
    if isinstance(prompt, str):
        route = "text"
    elif isinstance(prompt, list):
        route = "text" if prompt and all(isinstance(item, TextInput) for item in prompt) else "multimodal"
        if any(isinstance(item, LocalImageInput) for item in prompt):
            route = "image"
    else:
        raise TypeError(f"Not Supported Prompt Type: {type(prompt).__name__}")
    selection = CONFIG["codex"]["models"][route]
    return selection["model"], selection["provider"]


def _sandbox_from_config() -> Sandbox:
    attribute = CONFIG["codex"]["sandbox"].replace("-", "_")
    try:
        return getattr(Sandbox, attribute)
    except AttributeError as exc:
        raise RuntimeError(f"Not Supported Codex Sandbox: {CONFIG['codex']['sandbox']}") from exc


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
                "Codex Transient Error, to Retry: operation=%s, attempt=%s/%s, error=%s",
                operation_name, attempt, max_attempts, exc,
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            delay = min(max_delay_s, delay * backoff_multiplier)

    raise RuntimeError("unreachable")


def _error_event(exc: Exception, *, operation: str) -> RunEvent:
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

    logger.error("Codex Execute Failure: operation=%s, code=%s, error=%s", operation, code, exc)
    return {"type": "error", "code": code, "content": content}


async def _save_turn_usage(
    log: dict[str, Any],
    *,
    duration_ms: int | None = None,
    finalize: bool = False,
) -> None:
    """在有限时间内完成单条写入或耗时回填，抵御调用方重复取消。"""
    async def save() -> None:
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if finalize:
                    await finalize_usage(log["runId"], duration_ms)
                else:
                    await SavingLog(log=log)
        except Exception:
            # A commit may already have succeeded; do not retry and duplicate it.
            logger.exception("Token Usage Logs Saving Failure: user_id=%s, thread_id=%s",
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
    async def running(
        cls,
        user_id: str,
        prompt: str | list[InputItem],
        run_id: str | None = None,
        *,
        notify_started: bool = False,
    ) -> AsyncGenerator[RunEvent, None]:
        try:
            thread_id = await thread_manager.get_user_thread(user_id)
        except Exception as error:
            yield _error_event(error, operation="thread_binding_read")
            return

        model, model_provider = _resolve_model(prompt)

        workspace = Path(CONFIG["storage"]["workspaceRoot"]).expanduser() / user_id
        try:
            # 10 秒是项目默认策略，兼容尚未填写新字段的挂载配置。
            await prepare_workspace(workspace, CONFIG["codex"].get("gitInitTimeoutSeconds", 10))
        except Exception as error:
            yield _error_event(error, operation="workspace_init")
            return

        thread_config = {
            "cwd":str(workspace),
            # 来源：Codex shell_environment_policy.set；只覆盖当前用户的环境路径。
            "config": {f"shell_environment_policy.set.{key}": value
                       for key, value in workspace_environment(workspace).items()},
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
            running_timestamp = datetime.now(timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"])))
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

                    logger.warning("Restoring the thread (%s) failure, try to restore the thread from the archive.", e)

                    try:
                        thread = await _retry_on_overload_async(
                            lambda: codex.thread_unarchive(thread_id),
                            operation_name="thread_unarchive",
                        )
                        # 解归档本身不接收配置，重新恢复才能注入当前用户的依赖路径。
                        thread = await _retry_on_overload_async(
                            lambda: codex.thread_resume(thread_id=thread_id, **thread_config),
                            operation_name="thread_resume",
                        )
                    except Exception as error:
                        if is_retryable_error(error) or isinstance(
                            error, (InvalidParamsError, MethodNotFoundError)
                        ):
                            yield _error_event(error, operation="thread_unarchive")
                            return
                        logger.warning("Restoring the thread (%s) failure from the archive, try to create a new thread", error)
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
                await session_codex._initialize_session_name(user_id, thread, prompt)
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
            async with aclosing(cls._stream_turn(
                live, user_id, run_id, notify_started, running_timestamp, probe,
            )) as events:
                async for event in events:
                    yield event

    @classmethod
    async def _stream_turn(
        cls,
        live: LiveTurn,
        user_id: str,
        run_id: str | None,
        notify_started: bool,
        running_timestamp: datetime,
        probe: RunProbe | None,
    ) -> AsyncGenerator[RunEvent, None]:
        """转换单个 turn 的事件；由调用方在 SDK 会话关闭前完成收尾。"""
        thread, handle, model = live.thread, live.handle, live.model
        usage_run_id = run_id or uuid4().hex
        usage_received = False
        duration_ms = None
        completed = False
        interrupted = False
        message_phases = {}
        tool_output = {}
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
                            elif item.type not in {"userMessage", "reasoning"}:
                                # 工具参数、状态和结果均展示；已流出的命令输出不重复追加。
                                data = (item.model_dump(mode="json", exclude_none=True)
                                        if hasattr(item, "model_dump") else vars(item).copy())
                                output = data.pop("aggregated_output", data.pop("aggregatedOutput", None))
                                if event.method == "item/completed":
                                    streamed = tool_output.pop(item.id, "")
                                    if output and output.startswith(streamed):
                                        output = output[len(streamed):]
                                if output:
                                    data["output"] = output
                                label = "Tool Call Starting" if event.method == "item/started" else "Tool Call Ending"
                                yield {"type": "progress", "item_id": item.id,
                                       "content": "\n" + label + ": " + item.type + "\n"
                                       + json.dumps(data, ensure_ascii=False, default=str) + "\n"}

                        elif event.method == "item/commandExecution/outputDelta":
                            item_id = event.payload.item_id
                            delta = event.payload.delta
                            tool_output[item_id] = tool_output.get(item_id, "") + delta
                            yield {"type": "progress", "item_id": item_id, "content": delta}

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
                                    "timeStamp": datetime.now(running_timestamp.tzinfo).strftime(
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

                        elif event.method not in {"turn/completed", "turn/started"}:
                            # 其他运行时输出（例如 hook、工具进度、plan）保留原事件类型。
                            payload = event.payload
                            data = (payload.model_dump(mode="json", exclude_none=True)
                                    if hasattr(payload, "model_dump") else vars(payload))
                            yield {"type": "progress", "item_id": event.method,
                                   "content": event.method + "\n" + json.dumps(
                                       data, ensure_ascii=False, default=str) + "\n"}

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
                    yield _error_event(RuntimeError("The streaming events is turn/completed, but not received"),
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
                    await session_codex._sync_session_time(user_id, thread)
            finally:
                # 所有退出路径仅收尾一次；已知耗时时回填，不重复插入用量。
                if usage_received:
                    await _save_turn_usage({
                        "userId": user_id,
                        "threadId": thread.id,
                        "runId": usage_run_id,
                    }, duration_ms=duration_ms, finalize=True)

        yield {"type": "interrupted" if interrupted else "done"}
