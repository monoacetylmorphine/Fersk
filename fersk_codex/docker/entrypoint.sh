#!/bin/sh
set -eu

data_dir="${HOME}/.fersk"
mkdir -p "$data_dir" "${CODEX_HOME:-${HOME}/.codex}/workspace"

# 两个服务使用同一默认源，并发首次启动也不覆盖挂载配置。
python /opt/fersk_codex/configs/initialize_config.py

exec "$@"
