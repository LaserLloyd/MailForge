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

## Copyleft components

None at runtime. `html2text` (GPL-3.0-or-later) was replaced by `markdownify` (MIT) in 0.7.1;
see [`docs/LICENSING.md`](docs/LICENSING.md).

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
