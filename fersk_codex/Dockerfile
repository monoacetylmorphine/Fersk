# syntax=docker/dockerfile:1
FROM node:lts-bookworm-slim AS node-runtime
FROM python:3.13.15-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=node-runtime /usr/local /usr/local

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/fersk_codex/.venv/bin:${PATH}" \
    PYTHONPATH=/opt

# ffmpeg also provides ffprobe; git/ripgrep are useful to Codex in its workspace.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg git libstdc++6 ripgrep zip unzip \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

ARG APP_USER=app
ARG APP_UID=1000
ARG APP_GID=1000
RUN if ! getent group "${APP_GID}" > /dev/null; then \
        groupadd --gid "${APP_GID}" "${APP_USER}"; \
    fi \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir "/home/${APP_USER}" --shell /bin/bash "${APP_USER}"

ENV HOME=/home/${APP_USER}
ENV CODEX_HOME=${HOME}/.codex

WORKDIR /opt/fersk_codex
COPY fersk_codex/pyproject.toml fersk_codex/uv.lock ./
ARG PYTHON_PACKAGE_INDEX=https://pypi.org/simple
RUN uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python \
        --default-index "${PYTHON_PACKAGE_INDEX}" \
    && ln -s "$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" \
        /usr/local/bin/codex \
    && codex --version
COPY fersk_codex/ .
# Dependencies are cached above; run the copied source via PYTHONPATH.
RUN install -m 0755 docker/entrypoint.sh /usr/local/bin/fersk-entrypoint \
    && mkdir -p "${HOME}/.fersk" "${CODEX_HOME}/workspace" \
    && chown -R "${APP_UID}:${APP_GID}" "${HOME}"

USER ${APP_UID}:${APP_GID}
WORKDIR ${HOME}
ENTRYPOINT ["fersk-entrypoint"]
CMD ["python", "-m", "fersk_codex.gateway"]
