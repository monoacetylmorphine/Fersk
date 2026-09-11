"""任务状态与短期去重缓存；按所属任务释放，最长保留 24 小时。"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import time

from fersk_codex.utils.config_loader import CONFIG

# 来源：用户指定内存状态和待写日志最长保留 24 小时。
RETENTION_SECONDS = 24 * 60 * 60

@dataclass
class ActiveCodexRun:
    run_id: str
    chat_id: str
    message_ids: frozenset[str]
    interrupted: bool = False
    codex_started: bool = False
    controls: asyncio.Lock = field(default_factory=asyncio.Lock)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    target_id: str = ""
    probe: object | None = None
    task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None
    timeout_task: asyncio.Task | None = None
    notified: bool = False
    cards: object | None = None

    created_at: float = field(default_factory=time.monotonic)
    expired: bool = False
    detached: bool = False
    released: bool = False


class SessionCache:
    def __init__(self):
        for name in (
            "codex_locks", "reset_tasks", "buffered_events", "buffer_tasks",
            "reaction_message_ids", "chat_generations", "pending_chat_requests",
            "received_message_ids", "processed_message_ids", "received_at",
            "active_runs_by_message_id", "active_runs_by_chat", "all_runs", "blocked_chats",
        ):
            setattr(self, name, {})
        self.codex_locks_guard = asyncio.Lock()
        self.buffer_guard = asyncio.Lock()
        self.active_runs_guard = asyncio.Lock()
        self.reactions_being_cleared = set()
        self.recalled_message_ids = set()
        self._recall_times = {}
        self._reaction_times = {}
        self._users = {}
        self._buffer_times = {}

    @contextmanager
    def hold(self, chat_id):
        """在首次 await 前登记；包括等待锁、重置、历史请求的调用者。"""
        self._users[chat_id] = self._users.get(chat_id, 0) + 1
        try:
            yield
        finally:
            self._users[chat_id] -= 1
            if not self._users[chat_id]:
                self._users.pop(chat_id)
            self.release_idle(chat_id)

    def remember(self, mapping, chat_id, message_id):
        now = time.monotonic()
        entries = mapping.setdefault(chat_id, {})
        # 重复事件不会延长原消息的保留期限。
        entries.setdefault(message_id, now)
        for key, timestamp in list(entries.items()):
            if timestamp is not None and now - timestamp >= RETENTION_SECONDS:
                entries.pop(key, None)
        while len(entries) > CONFIG["messaging"]["recallCacheMaxEntries"]:
            entries.pop(next(iter(entries)))

    def recall(self, message_id):
        self.recalled_message_ids.add(message_id)
        self._recall_times.setdefault(message_id, time.monotonic())
        while len(self.recalled_message_ids) > CONFIG["messaging"]["recallCacheMaxEntries"]:
            oldest = next(iter(self._recall_times))
            self.recalled_message_ids.discard(oldest)
            self._recall_times.pop(oldest, None)

    def track_reaction(self, chat_id, message_id):
        self._reaction_times.setdefault((chat_id, message_id), time.monotonic())

    def release_messages(self, chat_id, message_ids, owner=None):
        """只清理本任务的瞬态数据；已转交的消息由接收方任务清理。"""
        for mid in message_ids:
            current = self.active_runs_by_message_id.get(mid)
            if current is not None and current is not owner:
                continue
            self.received_at.pop(mid, None)
            self.recalled_message_ids.discard(mid)
            self._recall_times.pop(mid, None)
            self.reaction_message_ids.get(chat_id, {}).pop(mid, None)
            self._reaction_times.pop((chat_id, mid), None)
        if not self.reaction_message_ids.get(chat_id):
            self.reaction_message_ids.pop(chat_id, None)

    def finish_run(self, state):
        self.release_messages(state.chat_id, state.message_ids, state)
        # 任务/流引用释放，阻塞状态只保留停止重试所需的元数据。
        state.task = None
        state.stop_task = None
        state.timeout_task = None
        state.cards = None
        self.release_idle(state.chat_id)

    def release_idle(self, chat_id):
        if (self._users.get(chat_id) or self.pending_chat_requests.get(chat_id)
                or chat_id in self.reset_tasks or chat_id in self.buffer_tasks
                or chat_id in self.buffered_events or chat_id in self.blocked_chats
                or chat_id in self.active_runs_by_chat
                or any(run.chat_id == chat_id for run in self.all_runs.values())
                or any(run.chat_id == chat_id for run in self.active_runs_by_message_id.values())):
            return
        lock = self.codex_locks.get(chat_id)
        if lock is not None and lock.locked():
            return
        self.codex_locks.pop(chat_id, None)
        self.chat_generations.pop(chat_id, None)
        self.release_messages(chat_id, set(self.received_message_ids.get(chat_id, {}))
                              | set(self.reaction_message_ids.get(chat_id, {})))

    def prune(self, now=None):
        """定期清理无后续消息的会话；返回需要关闭的过期任务。"""
        now = time.monotonic() if now is None else now
        for chat, timestamp in list(self._buffer_times.items()):
            if chat not in self.buffered_events or now - timestamp >= RETENTION_SECONDS:
                self._buffer_times.pop(chat, None)
                data = self.buffered_events.pop(chat, None)
                task = self.buffer_tasks.pop(chat, None)
                if task is not None and not task.done():
                    task.cancel()
                if data is not None:
                    self.release_messages(chat, {data.event.message.message_id})
        for mapping in (self.received_message_ids, self.processed_message_ids):
            for chat, entries in list(mapping.items()):
                for mid, timestamp in list(entries.items()):
                    if timestamp is not None and now - timestamp >= RETENTION_SECONDS:
                        entries.pop(mid, None)
                if not entries:
                    mapping.pop(chat, None)
        for mid, timestamp in list(self.received_at.items()):
            if now - timestamp >= RETENTION_SECONDS:
                self.received_at.pop(mid, None)
        for mid, timestamp in list(self._recall_times.items()):
            if mid not in self.recalled_message_ids or now - timestamp >= RETENTION_SECONDS:
                self.recalled_message_ids.discard(mid)
                self._recall_times.pop(mid, None)
        for (chat, mid), timestamp in list(self._reaction_times.items()):
            if (mid not in self.reaction_message_ids.get(chat, {})
                    or now - timestamp >= RETENTION_SECONDS):
                self.reaction_message_ids.get(chat, {}).pop(mid, None)
                self._reaction_times.pop((chat, mid), None)
                if not self.reaction_message_ids.get(chat):
                    self.reaction_message_ids.pop(chat, None)
        for chat in set(self.codex_locks) | set(self.chat_generations):
            self.release_idle(chat)
        runs = {run.run_id: run for run in (*self.all_runs.values(), *self.blocked_chats.values())}
        return [run for run in runs.values() if now - run.created_at >= RETENTION_SECONDS]

    def expire_run(self, state):
        """关闭尝试结束后，即使失败也按用户要求解除过期缓存。"""
        for mid, owner in list(self.active_runs_by_message_id.items()):
            if owner is state:
                self.active_runs_by_message_id.pop(mid, None)
        for mapping, key in ((self.all_runs, state.run_id),
                             (self.active_runs_by_chat, state.chat_id),
                             (self.blocked_chats, state.chat_id)):
            if mapping.get(key) is state:
                mapping.pop(key, None)
        self.finish_run(state)
