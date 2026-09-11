"""读取同一份运行配置，并使用共享 Schema 完整校验。"""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fersk_codex.configs.config_validation import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path.home() / ".fersk"
ENV_FILE = DATA_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)
CONFIG_FILE = Path(os.environ.get("FERSK_CONFIG_FILE", str(DATA_ROOT / "config.json"))).expanduser()
SCHEMA_FILE = PROJECT_ROOT / "configs" / "config_schema.json"


def _load_config(file_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path).expanduser()
    config = load_config(file_path, SCHEMA_FILE)
    # 仅解析本服务消费的持久化路径；MCP 字段不启动任何扩展。
    for key in ("runLogPath", "databasePath", "workspaceRoot"):
        path = Path(config["storage"][key]).expanduser()
        if not path.is_absolute():
            path = file_path.resolve().parent / path
        config["storage"][key] = str(path)
    return config

CONFIG = _load_config(CONFIG_FILE)
