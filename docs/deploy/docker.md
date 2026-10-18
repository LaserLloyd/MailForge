# Running MailForge in Docker

MailForge watches your mailboxes over IMAP, screens what arrives and writes
**draft** replies with a model running on your own LLM server. It never sends
without a human click in its web UI. The image runs the listener, the agent and
the UI in one process; all state is in one volume, `/data`. No GPU is needed.

```bash
git clone https://github.com/LaserLloyd/MailForge.git && cd MailForge          # scrub-ok: public repo URL  (or: git pull)
cp deploy/compose/.env.example deploy/compose/.env && chmod 0600 deploy/compose/.env
$EDITOR deploy/compose/.env        # set MAILFORGE_KEYRING_PASSWORD (see "Credentials")
docker compose -f deploy/compose/compose.yml up -d
docker compose -f deploy/compose/compose.yml logs app | grep "MailForge UI"    # the one-shot login URL
```

Open the printed `http://127.0.0.1:8090/?token=...` URL once; the token is
swapped for a signed session cookie. If you changed `HOST_PORT`, use that port
in the URL. Lost the URL (the token is single-use)?

```bash
docker compose -f deploy/compose/compose.yml exec app mailforge open --no-browser   # prints a fresh login URL
```

Next, add a real mailbox (interactive; see "Credentials"), or look around first
with fictional data and no mailbox:

```bash
docker compose -f deploy/compose/compose.yml run --rm -it app setup-wizard
docker compose -f deploy/compose/compose.yml run --rm -p 127.0.0.1:8091:8091 -e MAILFORGE_UI_PORT=8091 app demo --no-browser
```

## Ports

| Port | Where | What |
|---|---|---|
| 8090 | container, published as `${BIND_ADDR:-127.0.0.1}:${HOST_PORT:-8090}` | the approval UI and `/healthz` |

MailForge is a localhost-only app by design (one-shot login token, signed
cookie, Host/Origin guard). The compose file keeps that: it publishes the port
on the host's loopback. Setting `BIND_ADDR=0.0.0.0` exposes the UI to your
network; do that only behind a reverse proxy that authenticates users and, if
the proxy's name differs, list it in `UI_EXTRA_HOSTS`.

### Binding inside the container (the three variables)

Natively the UI binds `127.0.0.1` on a random persisted port and answers only
`Host: 127.0.0.1:PORT` / `localhost:PORT`. A container cannot be reached that
way, so the image sets three environment variables that relax exactly those
three things (unset, behaviour is unchanged):

| Variable | Image default | Meaning |
|---|---|---|
| `MAILFORGE_UI_HOST` | `0.0.0.0` | interface the UI binds (native default `127.0.0.1`) |
| `MAILFORGE_UI_PORT` | `8090` | fixed UI port instead of a random persisted one |
| `MAILFORGE_UI_ALLOWED_HOSTS` | set by compose | extra `Host` header values to accept, comma-separated `host:port`. The browser's `Host` carries the **published** port, so compose sets `localhost:${HOST_PORT}` and `127.0.0.1:${HOST_PORT}`; without this every page answers 403 whenever `HOST_PORT` is not 8090 |

## Configuration

`deploy/compose/.env` (copy `.env.example`; **never commit it**, keep it `0600`;
it is git-ignored and excluded from the build context, so it cannot end up in an image):

| Variable | Default | Meaning |
|---|---|---|
| `MAILFORGE_KEYRING_PASSWORD` | empty | Unlocks the encrypted credential store. See below. |
| `MAILFORGE_KEYRING_PASSWORD_FILE` | empty | Same, read from a file (a Docker/compose secret at `/run/secrets/...`). Wins over the plain variable. |
| `LLM_BASE_URL` | `http://host.docker.internal:1234/v1` | Your local LLM server **as the container sees it**. Reduced to `host:port` and exported as `MAILFORGE_LLM__LM_STUDIO_HOST` (MailForge talks to LM Studio / StudioForge through the LM Studio SDK). The environment beats `config.toml`, so if you prefer to keep `[llm] lm_studio_host` in the file, unset `LLM_BASE_URL` and set the host there. |
| `BIND_ADDR` / `HOST_PORT` / `UI_EXTRA_HOSTS` | `127.0.0.1` / `8090` / empty | Host side of the port mapping, as above. |
| `MAILFORGE_*` | | Any MailForge setting: `MAILFORGE_SECURITY__QUARANTINE_THRESHOLD=0.9` and so on. |
| `DATA_PATH` | named volume `mailforge-data` | A host directory (owned by UID 1000) to browse and back up. |
| `EXTRAS` | `llm rag` | Build-time extras. Add `security-ml` for the heavy guardrail models (torch; several GB). |
| `MEM_LIMIT` / `CPU_LIMIT` | `1g` / `2.0` | Resource limits. |

`config.toml` lives in the volume at `/data/config/config.toml`; create or edit
it with `setup-wizard` / `account-add`, or copy a file in:
`docker compose -f deploy/compose/compose.yml cp ./config.toml app:/data/config/config.toml`.

### Pointing it at StudioForge or LM Studio on the host

Inside a container `localhost` is the container. The compose file maps
`host.docker.internal` to the Docker host (Docker Desktop provides it; on Linux
engines the `host-gateway` entry does), and `LLM_BASE_URL` defaults to port
1234 there. The host's server must listen on an address the container can
reach, not only on the host's `127.0.0.1`. With the LLM unreachable MailForge
keeps running and parks new drafts as `DEFERRED_NO_LLM` until it returns.

## Credentials

Nothing secret is in the image, the compose file or the build context.

Mailbox passwords are stored by MailForge in an **encrypted file**
(`keyrings.cryptfile`: Argon2 + AES-GCM) at
`/data/xdg/python_keyring/cryptfile_pass.cfg` in the volume. A headless
container has no desktop keyring, so that file is unlocked with a password you
provide at run time:

* `MAILFORGE_KEYRING_PASSWORD` in `deploy/compose/.env` (`chmod 0600`), or
* `MAILFORGE_KEYRING_PASSWORD_FILE` pointing at a mounted secret.

Choose a long random value and keep a copy in your password manager. It is
**not recoverable**: lose it and the stored mailbox passwords are unreadable
(re-enter them with `setup-wizard`). A wrong password fails with
`MAC check failed`; no password and no terminal fails with a clear
"credential store is locked" error instead of hanging.

The image selects the keyring backend through `PYTHON_KEYRING_BACKEND` and a
small module, `deploy/container_keyring.py`, because `keyrings.cryptfile` can
only ask for its password interactively. Application code is unchanged.

Add a mailbox:

```bash
docker compose -f deploy/compose/compose.yml run --rm -it app setup-wizard      # prompts; password typed hidden
docker compose -f deploy/compose/compose.yml run --rm -T app account-add ... --password-stdin   # scripted
docker compose -f deploy/compose/compose.yml restart app                          # listeners start at boot
```

## Health

`GET /healthz` (inside the container, as `127.0.0.1:8090`) returns `200` when
every mailbox listener is healthy and `503` "degraded" otherwise, including on
a fresh install with no mailbox. The image's `HEALTHCHECK` treats both as up
and fails only when the server is unreachable or the database errors; use the
JSON (`docker compose exec app mailforge health`) for per-mailbox monitoring.

## Data, upgrades, removal

```bash
git pull && docker compose -f deploy/compose/compose.yml up -d --build      # upgrade; /data is kept, the DB migrates itself
docker compose -f deploy/compose/compose.yml down                           # stop; /data is kept
docker compose -f deploy/compose/compose.yml down -v                        # stop AND delete mail DB, config, credential store
```

Back up the volume (`/data`) **and** the keyring password together; either is
useless without the other.

## Image

Built `FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim` and run on
`python:3.12-slim-trixie` (same interpreter at the same path, uv not in the
runtime image), non-root UID 1000, `/data` volume, tini via `init: true`,
`HEALTHCHECK`. sqlite-vec is verified to load at build time. Tagged releases
are published as `ghcr.io/laserlloyd/mailforge:<version>` with a CycloneDX SBOM,
SHA256SUMS, a cosign keyless signature and build-provenance attestation (see
`.github/workflows/containers.yml`).
