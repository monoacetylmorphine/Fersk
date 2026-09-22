"""在挂载卷内原子初始化共享配置；两个容器可并发启动。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def initialize(default_file: str | Path, target: str | Path) -> None:
    target = Path(target)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".config-", delete=False) as file:
            temporary = Path(file.name)
            file.write(Path(default_file).read_bytes())
            file.flush()
            os.fsync(file.fileno())
        try:
            # 硬链接仅发布完整文件；目标已存在时绝不覆盖。
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    default_target = Path.home() / ".fersk/config.json"
    target = Path(os.getenv("FERSK_CONFIG_FILE", str(default_target))).expanduser()
    if target == default_target:
        initialize(Path(__file__).with_name("config_default.json"), target)
    elif not target.is_file():
        raise RuntimeError(f"指定的配置文件不存在: {target}")


if __name__ == "__main__":
    main()
