"""Load and validate the mounted runtime JSON configuration."""

import json
import math
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from jsonschema import Draft202012Validator, FormatChecker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path.home() / ".fersk"
ENV_FILE = DATA_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)
CONFIG_FILE = Path(os.environ.get("FERSK_CONFIG_FILE", str(DATA_ROOT / "config.json"))).expanduser()
SCHEMA_FILE = PROJECT_ROOT / "configs" / "config_schema.json"
TOKEN_USAGE_FILE = DATA_ROOT / "usage" / "token_usage.csv"


def _load_config(file_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path).expanduser()
    try:
        with file_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"config.json 格式错误: {exc}") from exc

    schema_path = SCHEMA_FILE
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise RuntimeError(f"无法加载配置 Schema: {schema_path}") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(config), key=lambda e: str(list(e.absolute_path)))
    if errors:
        # Do not include instance values: they may contain credentials or prompts.
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: 不符合 {error.validator} 约束"
            for error in errors
        )
        raise RuntimeError(f"配置校验失败 ({file_path}): {details}")

    def check_finite(value, path=""):
        if isinstance(value, float) and not math.isfinite(value):
            raise RuntimeError(f"配置校验失败 ({file_path}): {path} 必须是有限数值")
        if isinstance(value, dict):
            for key, item in value.items():
                check_finite(item, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_finite(item, f"{path}.{index}")
    check_finite(config)
    messaging = config["messaging"]
    direct, buffered = set(messaging["directTypes"]), set(messaging["bufferedTypes"])
    if direct & buffered:
        raise RuntimeError(f"配置校验失败 ({file_path}): messaging.directTypes 与 bufferedTypes 不得重叠")
    if direct | buffered != set(messaging["supportedTypes"]):
        raise RuntimeError(f"配置校验失败 ({file_path}): directTypes 与 bufferedTypes 并集必须等于 supportedTypes")
    commands = [messaging[key].strip().lower() for key in ("newThreadCommand", "stopThreadCommand")]
    if commands[0] == commands[1] or any(any(c.isspace() for c in messaging[key]) for key in ("newThreadCommand", "stopThreadCommand")):
        raise RuntimeError(f"配置校验失败 ({file_path}): 会话命令不得相同或包含空白")
    # Runtime relative paths are anchored to the mounted configuration directory.
    storage = config["storage"]
    storage.setdefault("tokenUsagePath", str(TOKEN_USAGE_FILE))
    for key in ("runLogPath", "databasePath", "workspaceRoot", "tokenUsagePath"):
        path = Path(storage[key]).expanduser()
        if not path.is_absolute():
            path = file_path.resolve().parent / path
        storage[key] = str(path)
    return config

CONFIG = _load_config(CONFIG_FILE)
