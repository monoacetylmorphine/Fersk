"""独立读取和校验本服务使用的运行配置。"""

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


def _load_config(file_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path).expanduser()
    schema_path = SCHEMA_FILE
    try:
        with file_path.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"config.json 格式错误: {exc}") from exc

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise RuntimeError(f"无法加载配置 Schema: {schema_path}") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(config), key=lambda e: str(list(e.absolute_path)))
    if errors:
        # 错误只包含字段位置和约束，不回显可能含凭据的配置值。
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: 不符合 {error.validator} 约束"
            for error in errors
        )
        raise RuntimeError(f"配置校验失败 ({file_path}): {details}")

    def check_finite(value, rules, path=""):
        if not rules:
            return
        if "$ref" in rules:
            reference = rules["$ref"]
            rules = schema
            for segment in reference.removeprefix("#/").split("/"):
                rules = rules[segment]
        if isinstance(value, float) and not math.isfinite(value):
            raise RuntimeError(f"配置校验失败 ({file_path}): {path} 必须是有限数值")
        if isinstance(value, dict):
            for key, item in value.items():
                child = rules.get("properties", {}).get(key, rules.get("additionalProperties", {}))
                if isinstance(child, dict):
                    check_finite(item, child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_finite(item, rules.get("items", {}), f"{path}.{index}")
    check_finite(config, schema)
    return config

CONFIG = _load_config(CONFIG_FILE)
