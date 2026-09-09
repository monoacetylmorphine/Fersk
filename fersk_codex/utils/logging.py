import csv
import aiosqlite
from typing import Dict, Any
from pathlib import Path

from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger

logger = get_logger("Usage")


DB_PATH = CONFIG["storage"]["databasePath"]
CSV_PATH = CONFIG["storage"]["tokenUsagePath"]
TABLE_NAME = CONFIG["logging"]["tokenUsageTable"]


DEFAULT_VALUES = {
    "timeStamp": "",
    "userId": "",
    "threadId": "",
    "model": "",
    "taskDuration_ms": 0,
    "cache_write_input_tokens": 0,
    "cached_input_tokens": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0,
    "runId": "",
}


REQUIRED_KEYS = tuple(DEFAULT_VALUES)


CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timeStamp TEXT NOT NULL,
    userId TEXT,
    threadId TEXT,
    model TEXT,
    taskDuration_ms INTEGER,
    cache_write_input_tokens INTEGER,
    cached_input_tokens INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    runId TEXT NOT NULL DEFAULT ''
)
"""


INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (
    timeStamp,
    userId,
    threadId,
    model,
    taskDuration_ms,
    cache_write_input_tokens,
    cached_input_tokens,
    input_tokens,
    output_tokens,
    reasoning_output_tokens,
    total_tokens,
    runId
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def ensure_keys(log: Dict[str, Any]) -> Dict[str, Any]:
    """校验日志字段，并为缺失字段填充默认值。"""
    fixed_log = log.copy()

    for key in REQUIRED_KEYS:
        if key not in fixed_log:
            fixed_log[key] = DEFAULT_VALUES[key]
            print(f"缺少字段 '{key}'，已填充默认值 {DEFAULT_VALUES[key]}")

    return fixed_log


async def export_to_csv() -> None:
    """异步从数据库导出所有记录到 storage.tokenUsagePath。"""
    csv_path = Path(CSV_PATH)

    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(DB_PATH) as db:
            # 查询所有数据（按插入顺序或按时间戳排序）
            async with db.execute(f"SELECT * FROM {TABLE_NAME} ORDER BY timeStamp") as cursor:
                # 获取列名（来自 cursor.description）
                columns = [desc[0] for desc in cursor.description]

                # 一次性获取所有行（如果数据量巨大，可改为流式写入）
                rows = await cursor.fetchall()

        # 写入 CSV
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # 写入表头
            writer.writerow(columns)
            # 写入数据行
            writer.writerows(rows)

        print(f"✅ 导出成功：{csv_path} (共 {len(rows)} 条记录)")

    except aiosqlite.Error as exc:
        print(f"❌ 数据库读取失败：{exc}")
        raise
    except OSError as exc:
        print(f"❌ 文件写入失败：{exc}")
        raise


async def SavingLog(log: Dict[str, Any]) -> None:
    """异步写入日志。"""
    fixed_log = ensure_keys(log)

    try:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                if (await cursor.fetchone())[0] != "wal":
                    raise aiosqlite.OperationalError("无法启用 SQLite WAL 模式")
            await db.execute(CREATE_TABLE_SQL)
            # 写事务串行化旧表检查和追加字段，避免并发首次写入重复迁移。
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(f"PRAGMA table_info({TABLE_NAME})") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "runId" not in columns:
                await db.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN runId TEXT NOT NULL DEFAULT ''")

            await db.execute(
                INSERT_SQL,
                (
                    fixed_log["timeStamp"],
                    fixed_log["userId"],
                    fixed_log["threadId"],
                    fixed_log["model"],
                    fixed_log["taskDuration_ms"],
                    fixed_log["cache_write_input_tokens"],
                    fixed_log["cached_input_tokens"],
                    fixed_log["input_tokens"],
                    fixed_log["output_tokens"],
                    fixed_log["reasoning_output_tokens"],
                    fixed_log["total_tokens"],
                    fixed_log["runId"],
                ),
            )

            await db.commit()
            logger.info("Usage : detail=%s", log)
    except aiosqlite.Error as exc:
        print(f"数据库操作失败：{exc}")
        raise


async def finalize_usage(run_id: str, duration_ms: int | None) -> None:
    """任务退出时回填已知耗时并导出一次，不新增用量记录。"""
    if duration_ms is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                f"UPDATE {TABLE_NAME} SET taskDuration_ms = ? WHERE runId = ?",
                (duration_ms, run_id),
            )
            await db.commit()
    await export_to_csv()
