# Private: producing the public edition

This file, `scripts/publish_public.py`, `scripts/scrub_check.py` and `tests/unit/test_scrub_check.py` are maintainer-only and are excluded from the published tree (the scanner's denylist IS the map of what must stay private).

## Producing the public edition

```bash
python3 scripts/publish_public.py                  # dry run: prints the plan
python3 scripts/publish_public.py --go             # build it
python3 scripts/publish_public.py --out /tmp/pub --zip /tmp/pub.zip --go
```

The build is five gated steps; any failure aborts before anything is published:

1. **Allowlisted copy.** Only the paths in `ALLOWLIST` (pyproject, uv.lock,
   README, LICENSE, CHANGELOG, `src/`, `tests/`, `docs/`, `installer/`,
   `packaging/`, `scripts/`, `.gitignore`, `.python-version`) are read, minus
   `DENY_PATTERNS` (databases, `.env`, backups, caches, virtualenvs, runtime
   token files, wheels). Nothing outside the allowlist can leak, because
   nothing outside it is ever opened.
2. **Scrub gate.** `scripts/scrub_check.py` runs over the produced tree.
3. **Tests.** `uv run --extra dev pytest -q` runs *inside* the produced tree, so
   what ships is what was tested.
4. **Zip.** Deterministic ordering and fixed timestamps, wrapped in a single
   `bluebox-<version>/` folder. Size and sha256 are printed.
5. **Report.** `<out>/PUBLISH-REPORT.md` records file count, hash, scrub result
   and test summary.

Every subprocess uses a fixed argv list; there is no shell anywhere.

---

## Scrub policy

`scripts/scrub_check.py` is stdlib-only so it can run inside a produced tree
with nothing installed:

```bash
python3 scripts/scrub_check.py src tests docs README.md   # exit 1 on any hit
python3 scripts/scrub_check.py --self-test                # prove the rules work
```

It flags personal names and paths, deployment-specific business identifiers,
infrastructure hostnames, private/CGNAT IP ranges, real email addresses, and
secret-shaped strings (`sk-…`, `AKIA…`, `ghp_…`, long hex/base64 blobs, PEM
headers, `api_key = "value"`).

Rules for keeping it useful:

* **Use documentation namespaces in examples and fixtures**: `example.com`,
  `example.net`, `example.org`, and the reserved TLDs `.example`, `.test`,
  `.invalid`, `.localhost`. People are `Alex Example` / `Sam Rivera`, orgs are
  `Northwind Studio` / `Acme Workshop`, sites are `main` / `shop` / `studio`,
  hosts are `imap.example.com:993` and `smtp.example.com:587`.
* **Two escape hatches, used sparingly.** End a line with
  `# scrub-check: allow`, or wrap a block in `# scrub-check: begin-allow` /
  `# scrub-check: end-allow`. They exist for the scanner's own rule literals and
  for test fixtures that must contain a sample of what they prove is caught.
  Every use is a hole in the gate — justify it in a comment.
* **The allowlist is three strings** (the attribution line and the MIT copyright
  holder). Do not grow it.
* Adding a rule means adding a sample to `SELF_TEST_CASES`; adding an exception
  means adding one to `SELF_TEST_CLEAN`. `tests/unit/test_scrub_check.py`
  enforces both, and asserts the repository itself is publishable.

---

---

## Release checklist

1. `uv run ruff check src tests scripts` — clean.
2. `uv run --extra dev pytest -q` — green.
3. `uv run python scripts/scrub_check.py --self-test` and then over
   `src tests docs scripts installer packaging README.md pyproject.toml` —
   clean.
4. Bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry (what changed,
   and anything an existing install must do).
5. `uv run python scripts/publish_public.py` (dry run) — check the file list.
6. `uv run python scripts/publish_public.py --go` — note the printed sha256.
7. Skim `<out>/PUBLISH-REPORT.md` and the produced `README.md` as a stranger
   would read it.
8. Unpack the zip somewhere clean and run `uv run bluebox demo` from it.
9. Publish the archive, and record the version + sha256 wherever it is offered
   for download.
