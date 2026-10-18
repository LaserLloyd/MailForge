#!/bin/sh
# MailForge container entrypoint.
#
#  * `docker compose run --rm app account-list` works: a first argument that is
#    not an executable (or starts with "-") is a mailforge subcommand.
#  * LLM_BASE_URL (e.g. http://host.docker.internal:1234/v1) is reduced to the
#    host:port MailForge's [llm] lm_studio_host wants and exported as
#    MAILFORGE_LLM__LM_STUDIO_HOST - unless that variable is already set. The
#    environment beats config.toml, so set the host in one place only.
#  * The encrypted credential store (keyrings.cryptfile) needs a password to
#    unlock. It is taken from MAILFORGE_KEYRING_PASSWORD, or read from the file
#    named by MAILFORGE_KEYRING_PASSWORD_FILE (Docker secrets: /run/secrets/...),
#    and handed to deploy/container_keyring.py (the image's keyring backend) as
#    MAILFORGE_KEYRING_KEY. It is never written to disk by this script.
set -eu

if [ -z "${MAILFORGE_LLM__LM_STUDIO_HOST:-}" ] && [ -n "${LLM_BASE_URL:-}" ]; then
    h="${LLM_BASE_URL#*://}"
    h="${h%%/*}"
    MAILFORGE_LLM__LM_STUDIO_HOST="$h"
    export MAILFORGE_LLM__LM_STUDIO_HOST
fi

if [ -n "${MAILFORGE_KEYRING_PASSWORD_FILE:-}" ]; then
    if [ -r "$MAILFORGE_KEYRING_PASSWORD_FILE" ]; then
        MAILFORGE_KEYRING_PASSWORD="$(cat "$MAILFORGE_KEYRING_PASSWORD_FILE")"
    else
        echo "mailforge: MAILFORGE_KEYRING_PASSWORD_FILE=$MAILFORGE_KEYRING_PASSWORD_FILE is not readable" >&2
    fi
fi
if [ -n "${MAILFORGE_KEYRING_PASSWORD:-}" ]; then
    MAILFORGE_KEYRING_KEY="$MAILFORGE_KEYRING_PASSWORD"
    export MAILFORGE_KEYRING_KEY
    unset MAILFORGE_KEYRING_PASSWORD
fi

if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ] || ! command -v "$1" >/dev/null 2>&1; then
    set -- mailforge "$@"
fi
exec "$@"
