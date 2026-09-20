"""持久化用户与 Codex 线程的绑定，复用 storage.databasePath 数据库。"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

from fersk_codex.utils.config_loader import CONFIG


DB_PATH = CONFIG["storage"]["databasePath"]


@asynccontextmanager
async def _connect() -> AsyncGenerator[aiosqlite.Connection]:
    """首次访问时自动建库建表，每次操作结束后关闭连接。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        async with db.execute("PRAGMA journal_mode=WAL") as cursor:
            if (await cursor.fetchone())[0] != "wal":
                raise aiosqlite.OperationalError("无法启用 SQLite WAL 模式")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_thread (
                user_id TEXT PRIMARY KEY NOT NULL,
                thread_id TEXT
            )
        """)
        yield db


async def get_user_thread(user_id: str) -> str | None:
    """查询用户绑定；用户不存在或绑定已重置时返回 None。"""
    async with _connect() as db:
        async with db.execute(
            "SELECT thread_id FROM user_thread WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row is not None else None


async def set_user_thread(user_id: str, thread_id: str | None) -> None:
    """原子新增或更新绑定；传入 None 持久化重置，提交成功后才返回。"""
    async with _connect() as db:
        await db.execute("""
            INSERT INTO user_thread (user_id, thread_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET thread_id = excluded.thread_id
        """, (user_id, thread_id))
        await db.commit()
