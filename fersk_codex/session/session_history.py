"""会话历史：与活跃绑定共用数据库，名称固定，SDK 时间用于最近使用排序。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import regex
from openai_codex import TextInput

from fersk_codex.configs.loader import CONFIG

if TYPE_CHECKING:
    from openai_codex import InputItem


DB_PATH = CONFIG["storage"]["databasePath"]


CREATE_TABLE_SQL = """
            CREATE TABLE IF NOT EXISTS session_history (
                user_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                thread_name TEXT,
                updated_at INTEGER,
                PRIMARY KEY (user_id, thread_id)
            )
        """

CREATE_INDEX_SQL = """
            CREATE INDEX IF NOT EXISTS idx_session_history_user_time
            ON session_history (user_id, updated_at DESC)
        """

REGISTER_SESSION_SQL = """
            INSERT INTO session_history (user_id, thread_id, thread_name)
            VALUES (?, ?, ?) ON CONFLICT(user_id, thread_id) DO NOTHING
        """

GET_SESSION_SQL = """
            SELECT user_id, thread_id, thread_name, updated_at
            FROM session_history WHERE user_id = ? AND thread_id = ?
        """

LIST_SESSIONS_SQL = """
            SELECT user_id, thread_id, thread_name, updated_at
            FROM session_history WHERE user_id = ?
            ORDER BY updated_at DESC, thread_id DESC
        """

CLAIM_SESSION_NAME_SQL = """
                UPDATE session_history SET thread_name = ?, updated_at = NULL
                WHERE user_id = ? AND thread_id = ? AND thread_name IS NULL
            """

UPDATE_SESSION_TIME_SQL = """
            UPDATE session_history SET updated_at = ?
            WHERE user_id = ? AND thread_id = ?
              AND (updated_at IS NULL OR updated_at < ?)
        """


@dataclass(frozen=True)
class SessionRecord:
    user_id: str
    thread_id: str
    thread_name: str | None
    updated_at: int | None


def make_thread_name(prompt: str | list[InputItem]) -> str | None:
    """按用户约定保留 15 个 Unicode 字素簇，超长追加省略号。"""
    text = prompt if isinstance(prompt, str) else " ".join(
        item.text for item in prompt if isinstance(item, TextInput)
    )
    text = " ".join(text.split())
    return _truncate_thread_name(text)


def _truncate_thread_name(text: str) -> str | None:
    # 只扫描到第 16 个字素簇，避免为长 prompt 构造完整字符列表。
    clusters = []
    for match in regex.finditer(r"\X", text):
        clusters.append(match.group())
        if len(clusters) == 16:
            return "".join(clusters[:15]) + "…"
    return "".join(clusters) or None


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        async with db.execute("PRAGMA journal_mode=WAL") as cursor:
            if (await cursor.fetchone())[0] != "wal":
                raise aiosqlite.OperationalError("无法启用 SQLite WAL 模式")
        await db.execute(CREATE_TABLE_SQL)
        await db.execute(CREATE_INDEX_SQL)
        await db.commit()
        yield db


async def register_session(user_id: str, thread_id: str, prompt: str | list[InputItem]) -> None:
    """仅新线程调用；先固定首次名称，失败重试不会用后续输入覆盖。"""
    async with _connect() as db:
        await db.execute(REGISTER_SESSION_SQL, (user_id, thread_id, make_thread_name(prompt)))
        await db.commit()


async def get_session(user_id: str, thread_id: str) -> SessionRecord | None:
    """通过可信用户身份校验归属，不接受仅凭 thread_id 的查询。"""
    async with _connect() as db:
        async with db.execute(GET_SESSION_SQL, (user_id, thread_id)) as cursor:
            row = await cursor.fetchone()
    return SessionRecord(*row) if row else None


async def list_sessions(user_id: str) -> list[SessionRecord]:
    """时间降序；同秒用线程 ID 稳定排序，尚无 SDK 时间的记录排在末尾。"""
    async with _connect() as db:
        async with db.execute(LIST_SESSIONS_SQL, (user_id,)) as cursor:
            return [SessionRecord(*row) for row in await cursor.fetchall()]


async def claim_session_name(
    user_id: str,
    thread_id: str,
    prompt: str | list[InputItem],
) -> SessionRecord | None:
    """纯附件线程收到首次文本时固定候选名称，并标记等待 SDK 命名。"""
    name = make_thread_name(prompt)
    if name:
        async with _connect() as db:
            await db.execute(CLAIM_SESSION_NAME_SQL, (name, user_id, thread_id))
            await db.commit()
    return await get_session(user_id, thread_id)


async def update_session_time(user_id: str, thread_id: str, updated_at: int) -> None:
    """只更新已有记录的 SDK 时间，不改名称；过期响应不能使时间倒退。"""
    if type(updated_at) is not int or updated_at < 0:
        raise ValueError("SDK updated_at 必须是非负整数时间戳")
    async with _connect() as db:
        await db.execute(UPDATE_SESSION_TIME_SQL, (updated_at, user_id, thread_id, updated_at))
        await db.commit()
