#!/bin/sh
# 从任意目录调用；仅首次创建仓库根目录 .env，不覆盖已有凭据。
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"

if [ "$#" -eq 0 ]; then
    printf '用法：%s <Compose 参数>，例如 build --no-cache 或 up -d\n' "$0" >&2
    exit 2
fi

command -v docker >/dev/null 2>&1 || {
    printf '错误：未安装 docker。\n' >&2
    exit 1
}
docker compose version >/dev/null

if [ -L .env ] || { [ -e .env ] && [ ! -f .env ]; }; then
    printf '错误：.env 必须是普通文件，不能是符号链接或目录。\n' >&2
    exit 1
fi

if [ ! -e .env ]; then
    command -v openssl >/dev/null 2>&1 || {
        printf '错误：首次初始化需要 openssl。\n' >&2
        exit 1
    }
    umask 077
    temporary_env=$(mktemp "$project_dir/.env.init.XXXXXX")
    trap 'rm -f -- "$temporary_env"' EXIT
    trap 'exit 1' HUP INT TERM

    # 长度来源：用户提供的初始化命令；NEXTAUTH_SECRET 使用 32 个随机字节。
    for key in POSTGRES_PASSWORD CLICKHOUSE_PASSWORD MINIO_ROOT_PASSWORD REDIS_AUTH SALT; do
        value=$(openssl rand -hex 16)
        printf '%s=%s\n' "$key" "$value" >> "$temporary_env"
    done
    value=$(openssl rand -base64 32)
    printf 'NEXTAUTH_SECRET=%s\n' "$value" >> "$temporary_env"
    value=$(openssl rand -hex 32)
    printf 'ENCRYPTION_KEY=%s\n' "$value" >> "$temporary_env"
    unset value
    chmod 600 "$temporary_env"

    # 原子发布完整文件；并发调用时只有一个进程成功，不替换已有 .env。
    if ln "$temporary_env" .env 2>/dev/null; then
        printf '已初始化 %s/.env（权限 600），请安全保存。\n' "$project_dir" >&2
    elif [ ! -f .env ] || [ -L .env ]; then
        printf '错误：无法创建 .env。\n' >&2
        exit 1
    fi
    rm -f -- "$temporary_env"
    trap - EXIT HUP INT TERM
fi

# 交给 Compose 解析 dotenv，不使用 source/eval；只校验，不打印凭据。
if ! docker compose --project-directory "$project_dir" --env-file "$project_dir/.env" \
    -f "$project_dir/docker-compose.yaml" config --quiet; then
    printf '配置校验失败；已有 .env 未改动。请检查缺失变量及 shell 中的空值覆盖。\n' >&2
    exit 1
fi

exec docker compose --project-directory "$project_dir" --env-file "$project_dir/.env" \
    -f "$project_dir/docker-compose.yaml" "$@"
