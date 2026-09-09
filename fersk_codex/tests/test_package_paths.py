"""Regression coverage for package relocation and mounted storage paths."""

import copy
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fersk_codex import FerskCodex, LiveTurn
from fersk_codex.core import codex
from fersk_codex.utils import logging as usage
from fersk_codex.utils.config_loader import CONFIG, _load_config


class PackagePathTests(unittest.TestCase):
    def test_public_exports_preserve_class_identity(self):
        self.assertIs(FerskCodex, codex.FerskCodex)
        self.assertIs(LiveTurn, codex.LiveTurn)

    def test_string_config_path_and_relative_storage(self):
        with TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            paths = {"runLogPath": "logs", "databasePath": "state.sqlite",
                     "tokenUsagePath": "usage/token_usage.csv", "workspaceRoot": "workspace"}
            config["storage"].update(paths)
            file_path = Path(directory) / "config.json"
            file_path.write_text(json.dumps(config), encoding="utf-8")
            loaded = _load_config(str(file_path))
            for key, relative in paths.items():
                self.assertEqual(Path(loaded["storage"][key]), file_path.parent.resolve() / relative)

    def test_existing_config_without_csv_setting(self):
        with TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            config["storage"].pop("tokenUsagePath", None)
            file_path = Path(directory) / "config.json"
            file_path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(_load_config(file_path)["storage"]["tokenUsagePath"],
                             str(Path.home() / ".fersk/usage/token_usage.csv"))

    def test_storage_paths_expand_current_home(self):
        with TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            paths = {"runLogPath": "~/.fersk/logs", "databasePath": "~/.fersk/state.sqlite",
                     "tokenUsagePath": "~/.fersk/usage/token_usage.csv",
                     "workspaceRoot": "~/.codex/workspace"}
            config["storage"].update(paths)
            file_path = Path(directory) / "config.json"
            file_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict("os.environ", HOME=str(Path(directory) / "another-user")):
                loaded = _load_config(file_path)
                for key, value in paths.items():
                    self.assertEqual(loaded["storage"][key], str(Path.home() / value[2:]))


class UsagePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_creates_separate_usage_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "usage" / "token_usage.csv"
            record = dict(usage.DEFAULT_VALUES, timeStamp="2026-09-08", userId="test-user")
            with patch.object(usage, "DB_PATH", root / "state.sqlite"), patch.object(
                usage, "CSV_PATH", csv_path
            ):
                await usage.SavingLog(record)
                self.assertFalse(csv_path.exists())
                await usage.finalize_usage(record["runId"], None)
            with csv_path.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]["userId"], "test-user")
            self.assertFalse((root / "token_usage.csv").exists())
