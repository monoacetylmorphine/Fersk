# syntax=docker/dockerfile:1
FROM node:lts-bookworm-slim AS node-runtime
FROM python:3.13.15-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=node-runtime /usr/local /usr/local

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/fersk_codex/.venv/bin:${PATH}" \
    PYTHONPATH=/opt

# 来源：现有音频流程及 skills-office；仅显式安装两套字体，禁用推荐包。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg git libstdc++6 ripgrep zip unzip \
        libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils pandoc \
        fontconfig fonts-inter fonts-noto-cjk \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && fc-cache -f

# 构建时安装 pnpm 最新版本；Office 业务包由用户工作区初始化器安装。
RUN --mount=type=cache,target=/root/.npm \
    npm install --global pnpm --no-audit --no-fund \
    && node --version && pnpm --version

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
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python \
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
CMD ["python", "-m", "fersk_codex.main"]
