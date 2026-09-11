"""Run probes and an append-only JSONL journal; no IM or SDK dependencies."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from itertools import groupby
from pathlib import Path
import threading
import time

from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger
from fersk_codex.middleware.session_cache import RETENTION_SECONDS

logger = get_logger("Watchdog")


def settings():
    return CONFIG["codex"]["watchdog"]


def should_log_event(method: str) -> bool:
    """Keep lifecycle notifications and final items, not streaming fragments."""
    return (not method.lower().endswith("delta")
            and (not method.startswith("item/") or method == "item/completed"))


def _summary_fields(source, fields):
    """只取白名单标量；500 字符上限是本项目的日志策略。"""
    result = {}
    for attribute, key in fields:
        value = getattr(source, attribute, None)
        value = getattr(value, "value", value)
        value = getattr(value, "root", value)
        if isinstance(value, str):
            result[key] = value[:500] + ("…" if len(value) > 500 else "")
        elif isinstance(value, (bool, int, float)):
            result[key] = value
    return result


def summarize_item(item):
    """终端与 JSONL 共用摘要，不序列化命令输出、工具结果或图片数据。"""
    result = _summary_fields(item, (
        ("id", "id"), ("type", "type"), ("status", "status"),
        ("duration_ms", "durationMs"), ("exit_code", "exitCode"),
    ))
    if item.type == "imageGeneration":
        result.update(_summary_fields(item, (
            ("saved_path", "savedPath"), ("transparent_background", "transparentBackground"),
        )))
    elif item.type == "agentMessage":
        result.update(_summary_fields(item, (("phase", "phase"),)))
        result["textLength"] = len(getattr(item, "text", "") or "")
    return result


def summarize_event(event):
    """事件仅输出定位及状态字段；包括 turn 内嵌 items 在内均不展开。"""
    payload = event.payload
    result = {"method": event.method, **_summary_fields(payload, (
        ("thread_id", "threadId"), ("turn_id", "turnId"), ("will_retry", "willRetry"),
    ))}
    if event.method == "item/completed":
        result["item"] = summarize_item(payload.item.root)
    turn = getattr(payload, "turn", None)
    if turn is not None:
        result["turn"] = _summary_fields(turn, (
            ("id", "id"), ("status", "status"), ("duration_ms", "durationMs"),
        ))
    error = getattr(payload, "error", None) or getattr(turn, "error", None)
    if error is not None:
        result["error"] = _summary_fields(error, (
            ("message", "message"), ("additional_details", "additionalDetails"),
        ))
    return result


class RunJournal:
    """One writer per process. Append one JSON object per line.

    ``path`` is a directory; records use their enqueue date in the configured
    timezone. Disk I/O runs in one background writer, and failed batches retry
    their original daily file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._timezone = timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"]))
        self._lock = threading.Lock()
        self._records = deque()
        self._revision = 0
        self._saved = 0
        self._discarded = 0
        self._thread = None

    def record(self, record):
        with self._lock:
            day = datetime.now(self._timezone).strftime("%Y-%m-%d")
            self._records.append((self.path / f"{day}_logs.jsonl", record, time.monotonic()))
            self._revision += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._write, daemon=True)
                self._thread.start()

    def _write_pending(self):
        with self._lock:
            expired = 0
            now = time.monotonic()
            while self._records and now - self._records[0][2] >= RETENTION_SECONDS:
                self._records.popleft()
                self._discarded += 1
                expired += 1
            records = list(self._records)
        if expired:
            logger.error("运行日志超过 24 小时仍未写入，已丢弃 %s 条；这些记录未落盘", expired)
        # Commit each contiguous date group separately. If a later file fails,
        # successful groups have already left the queue and cannot be duplicated.
        for path, group in groupby(records, key=lambda entry: entry[0]):
            batch = list(group)
            payload = "".join(json.dumps(record, ensure_ascii=False) + "\n"
                              for _, record, _ in batch).encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as file:
                offset = file.tell()
                try:
                    file.write(payload)
                    file.flush()
                except Exception:
                    file.truncate(offset)
                    raise
            with self._lock:
                for _ in batch:
                    self._records.popleft()
                self._saved += len(batch)

    def _write(self):
        while True:
            time.sleep(0.1)
            try:
                self._write_pending()
            except Exception:
                logger.exception("写入运行日志失败: %s", self.path)
                time.sleep(1)

    async def flush(self):
        with self._lock:
            target = self._revision
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            while True:
                with self._lock:
                    if self._saved + self._discarded >= target:
                        if self._discarded:
                            raise RuntimeError(f"有 {self._discarded} 条过期日志已丢弃，未全部写入")
                        return
                await asyncio.sleep(0.05)

journal = RunJournal(CONFIG["storage"]["runLogPath"])
probes = {}


@dataclass
class RunProbe:
    run_id: str
    chat_id: str
    message_ids: frozenset[str]
    received_at: float = field(default_factory=time.monotonic)
    phase: str = "queued"
    phase_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    last_event: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    terminal: str | None = None
    stop_reason: str | None = None
    stop_confirmed: bool = False
    cleanup_at: float | None = None
    cleanup_timed_out: bool = False
    tools: set[str] = field(default_factory=set)
    event_count: int = 0

    def record(self, event, **details):
        journal.record({
            "time": datetime.now(timezone.utc).isoformat(),
            "runId": self.run_id, "chatId": self.chat_id,
            "messageIds": sorted(self.message_ids),
            "threadId": self.thread_id, "turnId": self.turn_id,
            "phase": self.phase, "terminal": self.terminal,
            "stopReason": self.stop_reason, "stopConfirmed": self.stop_confirmed,
            "elapsedSeconds": round(time.monotonic() - self.received_at, 3),
            "idleSeconds": round(time.monotonic() - self.last_activity, 3),
            "lastEvent": self.last_event, "eventCount": self.event_count,
            "tools": sorted(self.tools), "event": event, **details,
        })

    def stage(self, phase):
        if self.phase == phase:
            return
        self.phase = phase
        self.phase_at = time.monotonic()
        self.record("stage")

    def finish(self, result):
        if self.terminal is None:
            self.terminal = result
            self.begin_cleanup()
            self.record("terminal")

    def begin_cleanup(self):
        """模型结束或收到停止请求后，开始独立的收尾计时。"""
        if self.cleanup_at is None:
            self.cleanup_at = time.monotonic()

    def activity(self, event):
        self.last_activity = time.monotonic()
        if should_log_event(event.method):
            self.last_event = event.method
        self.event_count += 1
        if event.method in {"item/started", "item/completed"}:
            item = event.payload.item.root
            if item.type in {"commandExecution", "mcpToolCall", "dynamicToolCall", "webSearch"}:
                if event.method == "item/started":
                    self.tools.add(item.id)
                else:
                    self.tools.discard(item.id)
            if event.method == "item/completed":
                self.record(event.method, item=summarize_item(item))
        if event.method == "turn/completed":
            status = event.payload.turn.status
            self.finish(self.stop_reason or getattr(status, "value", status))

    def expired(self, now=None):
        now = time.monotonic() if now is None else now
        if self.terminal or self.stop_reason:
            self.begin_cleanup()
            if now - self.cleanup_at >= settings().get("finalizationTimeoutSeconds", 30):
                return "cleanup_timeout"
            return None
        policy = settings()
        if now - self.received_at >= min(policy["maxRunSeconds"], RETENTION_SECONDS):
            return "run_timeout"
        if self.phase == "starting" and now - self.phase_at >= policy["startupTimeoutSeconds"]:
            return "startup_timeout"
        # Tool execution can legitimately be silent; the hard deadline remains.
        if (policy["idleTimeoutSeconds"] and self.phase == "running" and not self.tools
                and now - self.last_activity >= policy["idleTimeoutSeconds"]):
            return "idle_timeout"
        return None
