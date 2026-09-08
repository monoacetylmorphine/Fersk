"""SQLite/CSV persistence uses temporary files and preserves caller data."""

import csv
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fersk_codex.utils import logging as usage


class UsageLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.database = self.directory / 'state' / 'usage.sqlite'
        self.csv_path = self.directory / 'exports' / 'usage.csv'
        self.enterContext(patch.object(usage, 'DB_PATH', self.database))
        self.enterContext(patch.object(usage, 'CSV_PATH', self.csv_path))
        self.enterContext(patch.object(usage, 'logger'))
        self.enterContext(patch('builtins.print'))

    def test_ensure_keys_fills_defaults_without_mutating_input(self):
        original = {'userId': 'user', 'input_tokens': 42, 'custom': 'keep'}
        result = usage.ensure_keys(original)
        self.assertEqual(original, {'userId': 'user', 'input_tokens': 42, 'custom': 'keep'})
        self.assertEqual(result, dict(usage.DEFAULT_VALUES, **original))
        self.assertIsNot(result, original)

    async def test_save_preserves_numeric_fields_and_unicode_csv_escaping(self):
        record = dict(usage.DEFAULT_VALUES, timeStamp='2026-09-08', userId='用户,"one"\nnext',
                      threadId="thread'); DROP TABLE token_usage; --", model='model',
                      input_tokens=11, output_tokens=7, total_tokens=18, taskDuration_ms=123)
        before = record.copy()
        await usage.SavingLog(record)
        self.assertEqual(record, before)
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute(f'SELECT userId, threadId, input_tokens, output_tokens, total_tokens, taskDuration_ms FROM {usage.TABLE_NAME}').fetchone()
        self.assertEqual(row, (record['userId'], record['threadId'], 11, 7, 18, 123))
        with self.csv_path.open(newline='', encoding='utf-8') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['userId'], record['userId'])
        self.assertEqual(rows[0]['total_tokens'], '18')

    async def test_export_sorts_timestamps_and_retains_all_records(self):
        for timestamp, user in (('2026-09-09', 'later'), ('2026-09-08', 'earlier')):
            await usage.SavingLog(dict(usage.DEFAULT_VALUES, timeStamp=timestamp, userId=user))
        with self.csv_path.open(newline='', encoding='utf-8') as file:
            self.assertEqual([row['userId'] for row in csv.DictReader(file)], ['earlier', 'later'])
        await usage.export_to_csv()
        with self.csv_path.open(newline='', encoding='utf-8') as file:
            self.assertEqual(len(list(csv.DictReader(file))), 2)

    async def test_save_partial_record_persists_defaults(self):
        await usage.SavingLog({'userId': 'user'})
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute(f'SELECT userId, threadId, total_tokens FROM {usage.TABLE_NAME}').fetchone()
        self.assertEqual(row, ('user', '', 0))

    async def test_empty_table_exports_header(self):
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(usage.CREATE_TABLE_SQL)
        await usage.export_to_csv()
        with self.csv_path.open(newline='', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            self.assertEqual(list(reader), [])
            self.assertTrue(set(usage.REQUIRED_KEYS).issubset(reader.fieldnames))

    async def test_database_failure_propagates_and_does_not_export(self):
        self.database.parent.mkdir()
        self.database.write_bytes(b'not a SQLite database')
        with self.assertRaises(sqlite3.DatabaseError):
            await usage.SavingLog({'userId': 'user'})
        self.assertFalse(self.csv_path.exists())

    async def test_csv_failure_propagates_but_committed_record_is_retained(self):
        self.csv_path.mkdir(parents=True)
        with self.assertRaises(OSError):
            await usage.SavingLog(dict(usage.DEFAULT_VALUES, userId='retained'))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute(f'SELECT userId FROM {usage.TABLE_NAME}').fetchall(), [('retained',)])
