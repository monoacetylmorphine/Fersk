"""两个服务共用完整配置校验，不初始化客户端、不写入运行数据。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load_config(file_path: str | Path, schema_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path).expanduser()
    try:
        config = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"config.json 格式错误: {exc}") from exc
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise RuntimeError(f"无法加载配置 Schema: {schema_path}") from exc

    _ensure_finite_numbers(config, file_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(config), key=lambda e: str(list(e.absolute_path)))
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: 不符合 {error.validator} 约束"
            for error in errors
        )
        raise RuntimeError(f"配置校验失败 ({file_path}): {details}")
    _validate_messaging(config["messaging"])
    _validate_audio_limits(config["audio"]["limits"])
    return config


def _ensure_finite_numbers(value: Any, file_path: Path, path: str = '<root>') -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError(f"配置校验失败 ({file_path}): {path} 必须是有限数值")
    if isinstance(value, dict):
        for key, item in value.items():
            _ensure_finite_numbers(item, file_path, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite_numbers(item, file_path, f"{path}.{index}")


def _validate_messaging(messaging: dict[str, Any]) -> None:
    if any(character.isspace() for key in ("newThreadCommand", "stopThreadCommand")
           for character in messaging[key]):
        raise RuntimeError("配置校验失败: 会话命令不得包含空白")
    direct, buffered = set(messaging["directTypes"]), set(messaging["bufferedTypes"])
    if direct & buffered or direct | buffered != set(messaging["supportedTypes"]):
        raise RuntimeError("配置校验失败: directTypes 与 bufferedTypes 不得重叠，并集必须等于 supportedTypes")
    if messaging["newThreadCommand"].lower() == messaging["stopThreadCommand"].lower():
        raise RuntimeError("配置校验失败: 会话命令不得相同")


def _validate_audio_limits(limits: dict[str, Any]) -> None:
    if limits["targetBytes"] > limits["maxBytes"]:
        raise RuntimeError("配置校验失败: audio.limits.targetBytes 不得超过 maxBytes")
