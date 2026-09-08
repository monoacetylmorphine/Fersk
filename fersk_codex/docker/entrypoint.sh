#!/bin/sh
set -eu

data_dir="${HOME}/.fersk"
config_file="${FERSK_CONFIG_FILE:-${data_dir}/config.json}"
mkdir -p "$data_dir" "${CODEX_HOME:-${HOME}/.codex}/workspace"

# Only initialize the default runtime config; explicit custom paths must exist.
if [ "$config_file" = "${data_dir}/config.json" ] && [ ! -e "$config_file" ]; then
    # Do not overwrite an existing mounted configuration.
    (umask 077; cp -n /opt/fersk_codex/configs/config_default.json "$config_file")
    printf 'Initialized runtime config: %s\n' "$config_file"
fi

exec "$@"
