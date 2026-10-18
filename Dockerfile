# syntax=docker/dockerfile:1.7
# =============================================================================
#  MailForge - multi-stage container image
# =============================================================================
#  Build:   docker build -t mailforge:dev .
#  Run:     docker compose -f deploy/compose/compose.yml up -d
#
#  Load-bearing notes (same pattern as the DisPatch Chat image):
#   * BUILDER and RUNTIME ship the same interpreter at the same path - the uv
#     venv holds an absolute python symlink. The pair below is
#     ghcr.io/astral-sh/uv:python3.12-trixie-slim (built FROM
#     python:3.12-slim-trixie) + python:3.12-slim-trixie. 3.12 matches
#     .python-version.
#   * `--no-editable`: the package is installed INTO the venv; the runtime stage
#     copies only /app/.venv, no source tree.
#   * uv is NOT in the runtime image; tini comes from compose (`init: true`).
#   * NO secret is ever baked in: credentials live in an encrypted file in the
#     /data volume and are unlocked by a password supplied at run time (see
#     docs/deploy/docker.md).
#
#  Extras: `--build-arg EXTRAS="llm rag"` (default). `llm` is the LM Studio SDK
#  the bridge uses to reach StudioForge / LM Studio; `rag` adds sqlite-vec and
#  markitdown (document ingest). The `security-ml` extra pulls torch and
#  several GB of model code - add it only deliberately:
#     docker build --build-arg EXTRAS="llm rag security-ml" .
# =============================================================================


FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

ARG EXTRAS="llm rag"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; args=""; for e in $EXTRAS; do args="$args --extra $e"; done; \
    uv sync --locked --no-install-project --no-editable $args

# README.md because pyproject.toml names it as the package readme.
COPY README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; args=""; for e in $EXTRAS; do args="$args --extra $e"; done; \
    uv sync --locked --no-editable $args

RUN find /app/.venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /app/.venv -name '*.pyc' -delete


FROM python:3.12-slim-trixie AS runtime

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED

LABEL org.opencontainers.image.title="MailForge" \
      org.opencontainers.image.description="Local-LLM email assistant that triages, screens and drafts - and never auto-sends" \
      org.opencontainers.image.source="https://github.com/LaserLloyd/MailForge" \
      org.opencontainers.image.documentation="https://github.com/LaserLloyd/MailForge/blob/main/docs/deploy/docker.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# tzdata so TZ= resolves; ca-certificates for IMAP/SMTP over TLS and HTTPS.
# Every wheel in the lock is a prebuilt manylinux wheel: no compiler needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# UID/GID 1000: a bind-mounted host directory owned by the person running
# `docker compose up` is then writable without further ceremony.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chmod=0755 deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --chmod=0755 deploy/healthcheck.py /usr/local/bin/healthcheck.py
COPY deploy/container_keyring.py /usr/local/lib/mailforge/container_keyring.py

# Everything stateful lives under /data (one volume):
#   /data/config/config.toml   settings (no passwords)
#   /data/data/agent.db        the mail database (0600)
#   /data/xdg/python_keyring/  the encrypted credential store (cryptfile)
# The UI listens on 0.0.0.0:8090 INSIDE the container; compose publishes it on
# the host's loopback only, which preserves the app's "localhost-only" model.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data \
    IMAGE_VERSION=${VERSION} \
    MAILFORGE_HOME=/data \
    XDG_DATA_HOME=/data/xdg \
    XDG_CONFIG_HOME=/data/xdg-config \
    XDG_CACHE_HOME=/data/cache \
    NICEGUI_STORAGE_PATH=/data/data/.nicegui \
    TLDEXTRACT_CACHE=/data/cache/tldextract \
    MAILFORGE_UI_HOST=0.0.0.0 \
    MAILFORGE_UI_PORT=8090 \
    TMPDIR=/data/tmp \
    PYTHONPATH=/usr/local/lib/mailforge \
    PYTHON_KEYRING_BACKEND=container_keyring.EnvCryptFileKeyring

# No `VOLUME /data`: an anonymous volume would hide data loss.
RUN install -d -o app -g app -m 0700 /data /data/config /data/data /data/xdg /data/xdg-config /data/cache /data/tmp

# Fail the BUILD, not the first mail, if the vector extension cannot load.
# (A no-op when the rag extra was left out.)
COPY deploy/check_vec.py /tmp/check_vec.py
RUN python /tmp/check_vec.py && rm /tmp/check_vec.py

WORKDIR /data
USER app
EXPOSE 8090

# /healthz is outside the login gate but behind the Host guard; the script
# calls it as 127.0.0.1:8090. It treats "degraded" (no mailbox configured yet,
# or one listener down) as UP - only an unreachable server or a database error
# is unhealthy. See deploy/healthcheck.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "/usr/local/bin/healthcheck.py"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
