FROM node:22-bookworm-slim AS node-runtime

FROM python:3.13-slim-bookworm

# The mounted Langfuse tracing plugin executes a bundled .mjs hook (Node >=22).
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

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
COPY requirements.txt ./
ARG PYTHON_PACKAGE_INDEX=https://pypi.org/simple
RUN python -m pip install --index-url "${PYTHON_PACKAGE_INDEX}" -r requirements.txt \
    && ln -s "$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" \
        /usr/local/bin/codex \
    && codex --version
COPY . .
RUN python -m pip install --index-url "${PYTHON_PACKAGE_INDEX}" --no-deps . \
    && install -m 0755 docker/entrypoint.sh /usr/local/bin/fersk-entrypoint \
    && mkdir -p "${HOME}/.fersk" "${CODEX_HOME}/workspace" \
    && chown -R "${APP_UID}:${APP_GID}" "${HOME}"

USER ${APP_UID}:${APP_GID}
WORKDIR ${HOME}
ENTRYPOINT ["fersk-entrypoint"]
CMD ["python", "-m", "fersk_codex.gateway"]
