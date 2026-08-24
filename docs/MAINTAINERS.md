# Maintainers' guide

How this project is developed, kept publishable, and released.

---

## One codebase, two audiences

There is a single repository. Anything specific to a particular deployment —
site ids, brand names, drafting guidance, mailbox hosts, knowledge sources —
lives in **configuration**, never in code:

* `~/.config/openclaw-email/config.toml` (`%APPDATA%\openclaw-email\` on
  Windows), or `<OPENCLAW_EMAIL_HOME>/config/config.toml`;
* documents referenced from that file (knowledge roots, policy files, style
  guides).

The public edition is then produced *mechanically* from the repository. If you
find yourself about to type a real business name, mailbox, host, or path into a
source file or test, that is the signal it belongs in config instead.

---

## Producing the public edition

The maintainer builds the downloadable ZIP from this repository with an
allowlisted copy, a leak scanner, and a test run *inside* the produced tree,
then zips it with fixed timestamps. That tooling (and the scanner's word list)
is maintainer-private and is not part of this distribution — the contract it
enforces is the one stated above: examples and fixtures use documentation
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

Then point a mailbox at it (`openclaw-email account-add --site studio …`) and
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

## Tests and lint

```bash
uv run --extra dev pytest -q            # full suite
uv run ruff check .                     # exactly what CI runs
```

`tests/conftest.py` points `OPENCLAW_EMAIL_HOME` at a temporary directory before
the package is imported, so the suite can never read or write a real
configuration, and registers the example sites `main` + `shop` per test
(restoring the previous registry afterwards).

When touching security behaviour, remember the invariants the suite enforces:
`security.autosend_allowed` false, `security.require_human_approval` true, no
send path without a human approval record, quarantined and screened-out bodies
never reaching a model or the bridge.

---

## Releasing

Bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry saying what
changed and anything an existing install must do, and check the suite and lint
are green. The build and publishing steps that produce a release from this
repository are maintainer-private and are not part of this distribution.
