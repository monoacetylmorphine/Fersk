# syntax=docker/dockerfile:1
FROM python:3.13.15-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ARG APP_USER=app
ARG APP_UID=1000
ARG APP_GID=1000
ARG PYTHON_PACKAGE_INDEX=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/fersk_mcp/.venv/bin:${PATH}" \
    HOME=/home/${APP_USER} \
    CODEX_HOME=/home/${APP_USER}/.codex

WORKDIR /opt/fersk_mcp

COPY fersk_mcp/pyproject.toml fersk_mcp/uv.lock fersk_mcp/README.md ./
COPY fersk_mcp/__init__.py fersk_mcp/server.py ./
COPY fersk_codex/configs/config_default.json fersk_codex/configs/config_schema.json fersk_codex/configs/config_validation.py fersk_codex/configs/initialize_config.py /opt/fersk_codex/configs/
COPY fersk_mcp/configs ./configs
COPY fersk_codex/docker/entrypoint.sh /usr/local/bin/fersk-entrypoint
COPY fersk_mcp/tools ./tools
COPY fersk_mcp/utils ./utils

RUN chmod 0755 /usr/local/bin/fersk-entrypoint \
    && uv sync --locked --no-dev --no-editable --python /usr/local/bin/python \
        --default-index "${PYTHON_PACKAGE_INDEX}" \
    && if ! getent group "${APP_GID}" > /dev/null; then \
        groupadd --gid "${APP_GID}" "${APP_USER}"; \
    fi \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir "${HOME}" --shell /usr/sbin/nologin "${APP_USER}" \
    && mkdir -p "${HOME}/.fersk" "${CODEX_HOME}" \
    && chown -R "${APP_UID}:${APP_GID}" "${HOME}"

USER ${APP_UID}:${APP_GID}

# MCP 服务的默认 HTTP 端口；可使用 MCP_PORT 覆盖。
EXPOSE 8000

# 默认从 ${HOME}/.fersk/config.json 读取配置。
# 可挂载该目录，或使用 FERSK_CONFIG_FILE 指向另一份配置文件。
ENTRYPOINT ["fersk-entrypoint"]
CMD ["python", "-m", "fersk_mcp.server"]
