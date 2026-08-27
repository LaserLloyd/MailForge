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
   `mailforge-<version>/` folder. Size and sha256 are printed.
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

It flags home paths, infrastructure hostnames, private/CGNAT IP ranges, real
email addresses, assistant session URLs and trailers, and secret-shaped strings
(`sk-…`, `AKIA…`, `ghp_…`, long hex/base64 blobs, PEM headers,
`api_key = "value"`).

### The rules in the repo are generic; the literals are not in the repo

The scanner is a *tracked file*. Anything hardcoded in it ships with it — and
keeps shipping, in every revision of it that history holds. A denylist that
names the maintainer, the machines and the clients is therefore a publication
of exactly the list it exists to protect, and no amount of `.gitignore` after
the fact takes it back out of the log.

So the rules here describe **shapes**: an absolute home path, a tailnet
hostname, a private IP, a key, an address. The **literals** live in
`scripts/scrub-rules.local.txt` — git-ignored, mode 600, one case-insensitive
regex per line, `#` comments allowed:

```bash
chmod 600 scripts/scrub-rules.local.txt   # it is a list of what must not ship
python3 scripts/scrub_check.py --self-test
# → "... N private identifier rule(s) loaded from scrub-rules.local.txt"
```

A fork adds its own without editing the scanner. **CI never has that file**, so
a CI run cannot catch a name or a hostname — and the scanner says so on stderr
rather than printing "clean" and letting that read as "no private data". The
self-test proves the local-rule machinery against a throwaway file it writes
itself, with invented words, so no real identifier appears even in a fixture.

### The one deliberate personal name

MIT requires the copyright notice to survive verbatim, and naming a copyright
holder is a public authorship statement — the opposite of a leak. The exception
is scoped as narrowly as it can be:

* only in a file named `LICENSE` / `LICENCE` / `COPYING`,
* only on the line that *is* the copyright notice, and
* only against the **local** identifier rules.

Every generic rule still applies to that line. A hostname or an address in a
LICENSE is still a hit.

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
* **Never hardcode an identifier in `scrub_check.py`.** It is tracked; the
  literal ships. Put it in `scripts/scrub-rules.local.txt`.
* **Every self-test fixture is invented.** A sample that reaches for the real
  endpoint to prove the real endpoint is caught publishes the endpoint. The
  rules are shape rules, so a made-up sample of the right shape proves them
  exactly as well.
* **The allowlist is three strings** (the public attribution, in prose, URL and
  package-metadata form). Do not grow it.
* Adding a rule means adding a sample to `SELF_TEST_CASES`; adding an exception
  means adding one to `SELF_TEST_CLEAN`. `tests/unit/test_scrub_check.py`
  enforces both, and asserts the repository itself is publishable.

---

---

## Release checklist

1. `uv run ruff check src tests scripts` — clean.
2. `uv run --extra dev pytest -q` — green.
3. `uv run python scripts/scrub_check.py --self-test` — check the line it
   prints actually reports N private identifier rules loaded; zero means
   `scripts/scrub-rules.local.txt` is missing and the run proved less than it
   looks. Then run it over
   `src tests docs scripts installer packaging README.md pyproject.toml` —
   clean.
4. Bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry (what changed,
   and anything an existing install must do).
5. `uv run python scripts/publish_public.py` (dry run) — check the file list.
6. `uv run python scripts/publish_public.py --go` — note the printed sha256.
7. Skim `<out>/PUBLISH-REPORT.md` and the produced `README.md` as a stranger
   would read it.
8. Unpack the zip somewhere clean and run `uv run mailforge demo` from it.
9. Publish the archive, and record the version + sha256 wherever it is offered
   for download.
