# Maintainers' guide

How this project is developed, kept publishable, and released.

---

## One codebase, two audiences

There is a single repository. Anything specific to a particular deployment —
site ids, brand names, drafting guidance, mailbox hosts, knowledge sources —
lives in **configuration**, never in code:

* `~/.config/mailforge/config.toml` (`%APPDATA%\mailforge\` on
  Windows), or `<MAILFORGE_HOME>/config/config.toml`;
* documents referenced from that file (knowledge roots, policy files, style
  guides).

The public edition is then produced *mechanically* from the repository. If you
find yourself about to type a real business name, mailbox, host, or path into a
source file or test, that is the signal it belongs in config instead.

---

## Producing the public edition

Anything published from this repository is checked by
`scripts/scrub_check.py` (the same scanner the hooks and CI run) before it
ships. The scanner, the hooks and the CI workflow live in the git repository;
the downloadable archive leaves them out. The contract it enforces is the one stated above: examples and fixtures use documentation
namespaces (`example.com`, `example.net`, `.test`, `.invalid`), people are
`Alex Example` / `Sam Rivera`, organisations are `Northwind Studio` /
`Acme Workshop`, sites are `main` / `shop` / `studio`, hosts are
`imap.example.com:993` / `smtp.example.com:587`. Anything deployment-specific
belongs in configuration, not in a source file or a test.

---

## Adding a site

A *site* is one brand/business; every mailbox belongs to exactly one, and
templates, retrieval and the bridge are all site-scoped. Adding one is
configuration only — no code, no migration:

```toml
[sites.studio]
name = "Northwind Studio"
guidance = "Do not invent prices, stock, lead times, or delivery dates."
screening_mode = "standard"        # or "content_only"
triage_guidance = ""
```

Then point a mailbox at it (`mailforge account-add --site studio …`) and
restart the service so the listener starts. On first start the store registers
the configured ids; a legacy database whose `site_id` CHECK constraint predates
the registry is rebuilt lazily to accept them.

Notes:

* The response-template table is seeded **once**, on a brand-new database, with
  the neutral examples in `response_templates.EXAMPLE_TEMPLATES` — one copy per
  configured site. Existing databases are never re-seeded, so a site added later
  starts empty; create its templates in the Templates page.
* `screening_mode = "content_only"` gets its vocabulary from configuration
  (topical terms and the site's own name) with a generic packaged default, so no
  brand vocabulary belongs in `security/inbound_screening.py`.

---

## `llm.lm_studio_host` is a generic key, not literally LM Studio

`[llm] lm_studio_host` (and `LMStudioBridge`, the code path it feeds) predates
the OpenAI-compatible `/chat/completions` contract becoming the common
denominator across local inference servers. The value can be any
OpenAI-compatible chat-completions endpoint; do not assume it runs LM Studio.

`LMStudioBridge._post_chat` therefore sends an identity header
(`X-SF-Client: mailforge`) and a scheduling hint (`priority: 2`) on every chat
request. A plain LM Studio backend ignores both; a backend with request-tier
scheduling uses them to tell its co-tenants apart.

### Pointing it at a StudioForge gateway

* **Address.** `lm_studio_host = "127.0.0.1:1234"` is the OpenAI-compatible
  gateway. The StudioForge *panel* is a different port (8080 by default) and
  does not serve `/v1`; do not use it here or in `heavy_lift_base_url`.
* **API key.** When the gateway enforces keys, export the key in the variable
  named by `[llm] api_key_env` (default `MAILFORGE_LLM_API_KEY`; the shared name
  `STUDIOFORGE_API_KEY` is honoured as a fallback). Every request to
  `lm_studio_host` then carries `Authorization: Bearer <key>`. The key lives in
  the environment only - never in `config.toml`, the keyring, logs or the UI. The
  heavy-lift endpoint has its own key (`heavy_lift_key_env`) and never receives
  this one.
* **Model ids.** StudioForge ids are `publisher/repo/stem`, for example
  `unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S`. The shipped defaults are LM
  Studio catalogue ids and are answered with HTTP 404, so against StudioForge
  set `chat_model` and `embedding_model` to ids the server lists. `mailforge
  llm-check` compares the configured ids with `/v1/models` and says so plainly;
  the Models page and `mailforge bridge status` carry the same message.
* **Contract.** `tests/contract_studioforge/` pins what MailForge reads from
  and sends to the server. A change to StudioForge that breaks it should fail
  there first.

---

## Tests and lint

```bash
uv run --extra dev pytest -q            # full suite
uv run ruff check .                     # exactly what CI runs
```

`tests/conftest.py` points `MAILFORGE_HOME` at a temporary directory before
the package is imported, so the suite can never read or write a real
configuration, and registers the example sites `main` + `shop` per test
(restoring the previous registry afterwards).

The suite is also **hermetic**. `tests/_netguard.py` is installed first thing and
refuses every outbound connection except to ephemeral loopback ports a test
opens itself (so never the live StudioForge on 1234, a hosted API, or a made-up
LAN name); a test that attempts one fails even if the code under test swallowed
the error. Every test runs on the heuristic fallbacks of the optional
`security-ml` tier whatever extras are installed; tests that really use the
models carry `@pytest.mark.ml` and skip unless the weights are already in the
local Hugging Face cache. Set `NETGUARD_TRACE=1` to print the stack of a refused
connection.

When touching security behaviour, remember the invariants the suite enforces:
`security.autosend_allowed` false, `security.require_human_approval` true, no
send path without a human approval record, quarantined and screened-out bodies
never reaching a model or the bridge.

---

## Releasing

Bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry saying what
changed and anything an existing install must do, and check the suite and lint
are green, and that `python3 scripts/scrub_check.py` reports clean.
