"""读取同一份运行配置，并使用共享 Schema 完整校验。"""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fersk_mcp.configs.config_validation import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path.home() / ".fersk"
ENV_FILE = DATA_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)
CONFIG_FILE = Path(os.environ.get("FERSK_CONFIG_FILE", str(DATA_ROOT / "config.json"))).expanduser()
SCHEMA_FILE = PROJECT_ROOT / "configs" / "config_schema.json"


def _load_config(file_path: str | Path) -> dict[str, Any]:
    file_path = Path(file_path).expanduser()
    config = load_config(file_path, SCHEMA_FILE)
    return config

CONFIG = _load_config(CONFIG_FILE)
