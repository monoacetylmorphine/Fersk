#!/bin/sh
set -eu

data_dir="${HOME}/.fersk"
mkdir -p "$data_dir" "${CODEX_HOME:-${HOME}/.codex}/workspace"

# Both services use the same defaults and never overwrite mounted configuration, even on concurrent first startup.
python /opt/fersk_codex/configs/initialization.py

exec "$@"
