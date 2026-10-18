# Third-party notices

MailForge itself is MIT-licensed (see [`LICENSE`](LICENSE)). It depends on the
open-source packages below. This file lists the ones that need more than the
usual "MIT/BSD/Apache, keep the notice" treatment, and the direct runtime
dependencies with their licences as declared in the installed package metadata
(checked against `uv.lock`, 2026-10).

A complete, per-release list of every transitive package should be generated
from the lock file when an installer or SBOM is built (for example
`uv export --no-dev` plus a licence report); this file is the human-written
part.

## Copyleft component: html2text (GPL-3.0-or-later)

| | |
|---|---|
| Package | `html2text` 2025.4.15 (`pyproject.toml`: `html2text>=2024.2.26`; locked in `uv.lock`) |
| Licence | **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`) |
| Upstream | <https://github.com/Alir3z4/html2text/> (licence text: `COPYING` in the package) |
| Used by | `src/mailforge/mail/sanitize.py` (`import html2text`, `_make_html2text()`, final stage of the HTML-to-text pipeline) |
| Dependency kind | Core runtime dependency (always installed, always imported) |
| Shipped in | The PyInstaller folder bundle and the Inno Setup installer (`installer/`, `packaging/`) |

**Decision (owner, 2026-10): html2text stays for now.** What that means for
distribution is written down in [`docs/LICENSING.md`](docs/LICENSING.md), together
with the MIT-licensed replacements that were considered and the files that would
change. Until that decision is revisited, any binary distribution of MailForge
that bundles html2text must follow the obligations listed there (GPL text and
notice shipped with the binary, corresponding source offered).

## Other notable licences

| Package | Licence | Note |
|---|---|---|
| `pyinstaller` | GPL-2.0-or-later **with the bootloader exception** | Optional (`package` extra), build-time tool only. The exception lets the executables it produces be distributed under any licence; the PyInstaller bootloader itself is not a MailForge runtime dependency. |
| `typer`, `pydantic`, `pydantic-settings`, `keyring`, `keyrings.cryptfile`, `nicegui`, `markdown2`, `tomli-w`, `platformdirs`, `lmstudio`, `markitdown` | MIT | |
| `bleach`, `imap-tools`, `ftfy`, `python-multipart` | Apache-2.0 | Keep the `NOTICE`/licence text when redistributing. |
| `lxml_html_clean`, `tldextract`, `httpx`, `starlette` | BSD-3-Clause | |
| `sqlite-vec` | MIT or Apache-2.0 (dual) | Optional (`rag` extra). |
| `torch`, `transformers` (optional `security-ml` extra) | BSD-3-Clause / Apache-2.0 | |
| `llm-guard`, `presidio-analyzer`, `presidio-anonymizer`, `garak`, `detect-secrets` | MIT / Apache-2.0 | Optional extras. `invariant-ai` (optional `policy` extra) declares no licence field in its metadata: confirm before shipping it. |

## Model weights are not covered by this file

The optional `security-ml` tier downloads model weights from Hugging Face
(for example Llama Prompt Guard 2). Those weights carry their own licences
(for instance the Llama community licence), which apply to whoever downloads and
runs them. MailForge never bundles weights, and the test-suite never downloads
them.
