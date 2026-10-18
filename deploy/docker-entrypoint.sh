#!/bin/sh
# MailForge container entrypoint. Deliberately POSIX sh and short.
#
# In order:
#  1. If started as root: align the `app` account with PUID/PGID, chown /data,
#     drop privileges (setpriv) and re-exec. Not root: nothing to adjust.
#  2. Fail LOUD and EARLY when /data is not writable (bind-mount UID mismatch).
#  3. Pre-upgrade backup: when the image version differs from the one that last
#     ran on this volume, and BEFORE the app (and so any schema migration)
#     starts, copy the SQLite database (+ -wal/-shm), config.toml and the
#     credential store to /data/backups/<old>-to-<new>-<timestamp>/ (keep the
#     last 5). The app is not running yet, so the copy is consistent.
#  4. LLM_BASE_URL (e.g. http://host.docker.internal:1234/v1) is reduced to the
#     host:port MailForge's [llm] lm_studio_host wants and exported as
#     MAILFORGE_LLM__LM_STUDIO_HOST, unless that variable is already set.
#  5. The encrypted credential store needs a password: MAILFORGE_KEYRING_PASSWORD,
#     or the file named by MAILFORGE_KEYRING_PASSWORD_FILE (a Docker secret), is
#     handed to deploy/container_keyring.py as MAILFORGE_KEYRING_KEY. Never
#     written to disk by this script.
#  6. `docker compose run --rm app account-list` works: a first argument that is
#     not an executable (or starts with "-") is a mailforge subcommand.
set -eu

DATA_DIR=/data
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

log() { printf '%s mailforge-entrypoint: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ---- 1. privileges ----------------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    log "started as root; chown ${DATA_DIR} to ${PUID}:${PGID} and drop privileges"
    if [ "$(id -u app)" != "$PUID" ] || [ "$(id -g app)" != "$PGID" ]; then
        groupmod -o -g "$PGID" app 2>/dev/null || true
        usermod -o -u "$PUID" -g "$PGID" app 2>/dev/null || true
    fi
    mkdir -p "$DATA_DIR"
    chown "$PUID:$PGID" "$DATA_DIR"
    # Recurse only when something inside is visibly wrong, never on every boot.
    if [ "$(find "$DATA_DIR" -maxdepth 2 ! -user "$PUID" 2>/dev/null | head -n1)" != "" ]; then
        log "ownership mismatch on existing data: one-time recursive chown"
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups --inh-caps=-all -- "$0" "$@"
fi

# ---- 2. writability --------------------------------------------------------
if [ ! -w "$DATA_DIR" ]; then
    log "${DATA_DIR} is not writable by UID $(id -u) (owned by $(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null || echo '?'))."
    log "  Bind mount owned by another user? chown it to $(id -u):$(id -g), or use the PUID/PGID"
    log "  override (docs/deploy/docker.md, 'Volume permissions'), or use the named volume."
    die "data directory not writable"
fi
mkdir -p "$DATA_DIR/config" "$DATA_DIR/data" "$DATA_DIR/tmp" "$DATA_DIR/cache"

# ---- 3. pre-upgrade backup --------------------------------------------------
state_files() {
    find "$DATA_DIR" \( -path "$DATA_DIR/backups" -o -path "$DATA_DIR/cache" -o -path "$DATA_DIR/tmp" \) -prune -o \
        -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.sqlite*' \
                   -o -name 'config.toml' -o -name 'cryptfile_pass.cfg' -o -name 'ui_storage_secret' \) -print 2>/dev/null
}

pre_upgrade_backup() {
    marker="$DATA_DIR/.image-version"
    new="$(python -c "import importlib.metadata as m; print(m.version('mailforge'))" 2>/dev/null || echo unknown)-${IMAGE_VERSION:-dev}"
    old="$(cat "$marker" 2>/dev/null || true)"
    if [ "$old" != "$new" ]; then
        files="$(state_files)"
        if [ -n "$files" ]; then
            tag_old="$(printf '%s' "${old:-unversioned}" | tr -c 'A-Za-z0-9._+\n-' '_')"
            tag_new="$(printf '%s' "$new" | tr -c 'A-Za-z0-9._+\n-' '_')"
            dest="$DATA_DIR/backups/${tag_old}-to-${tag_new}-$(date -u '+%Y%m%dT%H%M%SZ')"
            umask 077
            mkdir -p "$dest"
            printf '%s\n' "$files" | while IFS= read -r f; do
                rel="${f#"$DATA_DIR"/}"
                mkdir -p "$dest/$(dirname "$rel")"
                cp -p "$f" "$dest/$rel"
            done
            log "image version changed (${old:-none} -> ${new}): database, config and credential store copied to ${dest}"
            ls -1dt "$DATA_DIR"/backups/*/ 2>/dev/null | tail -n +6 | while IFS= read -r d; do rm -rf "$d"; done
        fi
        printf '%s\n' "$new" > "$marker"
    fi
}
pre_upgrade_backup

# ---- 4. LLM host ------------------------------------------------------------
if [ -z "${MAILFORGE_LLM__LM_STUDIO_HOST:-}" ] && [ -n "${LLM_BASE_URL:-}" ]; then
    h="${LLM_BASE_URL#*://}"
    h="${h%%/*}"
    MAILFORGE_LLM__LM_STUDIO_HOST="$h"
    export MAILFORGE_LLM__LM_STUDIO_HOST
fi

# ---- 5. credential-store password -----------------------------------------------
if [ -n "${MAILFORGE_KEYRING_PASSWORD_FILE:-}" ]; then
    if [ -r "$MAILFORGE_KEYRING_PASSWORD_FILE" ]; then
        MAILFORGE_KEYRING_PASSWORD="$(cat "$MAILFORGE_KEYRING_PASSWORD_FILE")"
    else
        log "MAILFORGE_KEYRING_PASSWORD_FILE=$MAILFORGE_KEYRING_PASSWORD_FILE is not readable"
    fi
fi
if [ -n "${MAILFORGE_KEYRING_PASSWORD:-}" ]; then
    MAILFORGE_KEYRING_KEY="$MAILFORGE_KEYRING_PASSWORD"
    export MAILFORGE_KEYRING_KEY
    unset MAILFORGE_KEYRING_PASSWORD
fi

# ---- 6. command ---------------------------------------------------------------
if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ] || ! command -v "$1" >/dev/null 2>&1; then
    set -- mailforge "$@"
fi
log "data=${DATA_DIR} uid=$(id -u) gid=$(id -g) version=${IMAGE_VERSION:-dev}"
# exec: the app becomes the process that receives SIGTERM (clean SQLite close).
exec "$@"
