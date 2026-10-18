# Licensing notes: the GPL dependency (`html2text`)

This is an engineering note for the maintainer, not legal advice. It records what
was found, what the owner decided, and what each option would cost.

## The facts

- MailForge is MIT-licensed (`LICENSE`).
- `html2text` (GPL-3.0-or-later) is a **core** dependency: `pyproject.toml:26`,
  imported at `src/mailforge/mail/sanitize.py:18`, called in
  `_make_html2text()` (line 54) and `sanitize_html()` (line 84). Every install
  has it; the HTML-to-text step of the inbound pipeline cannot run without it.
- MailForge is distributed three ways: from source (git clone / `uv sync`), as a
  PyInstaller folder bundle, and as an Inno Setup Windows installer
  (`installer/inno/mailforge.iss`). The last two put `html2text` **inside** the
  artifact, in the same process as MailForge's own code.

## Owner decision (2026-10)

**Do not replace `html2text` now.** The dependency stays; this note and
`THIRD_PARTY_NOTICES.md` are the mitigation. Revisit before the first public
binary release.

## What it means for each distribution channel

| Channel | What is distributed | Consequence |
|---|---|---|
| Source checkout, `uv sync` / `pip install` from the repository | MailForge's own MIT source only. The user's machine downloads `html2text` from PyPI under its own licence. | Low risk: MailForge distributes no GPL code. The combination exists only on the user's computer. |
| **PyInstaller bundle / Inno installer** | MailForge **and** `html2text` in one executable tree. | Distributing GPL-3.0 code means the GPL's conditions attach to that distribution. The conservative reading (the FSF's) is that a program that imports a GPL library in one process forms a combined work which, **as distributed**, must be offered under GPL-3.0-or-later terms as a whole. The MIT licence on MailForge's own files does not prevent that (MIT is GPL-compatible); it means the binary cannot be handed out under MIT-only terms. |

### Obligations if a binary containing `html2text` is distributed

1. Ship the GPL-3.0 text and `html2text`'s copyright/licence notice
   (`COPYING`, `AUTHORS.rst` from the package) inside the installer, and show the
   notice in the installer or the About page.
2. Make the **corresponding source** available to recipients: MailForge's source
   at the released tag and the exact `html2text` source (the sdist pinned in
   `uv.lock`). A written offer valid for three years, or a link to the tagged
   release and the pinned sdist, meets this.
3. Describe the binary honestly: "MailForge (MIT) bundled with html2text
   (GPL-3.0-or-later); the combined installer is distributed under GPL-3.0-or-later
   terms." Do not attach extra restrictions (for example an EULA that forbids
   redistribution) to the installer.
4. Keep `THIRD_PARTY_NOTICES.md` current; M2's SBOM work should list the licence
   per package.

None of these obligations apply to users who install from source.

## Replacement options (for the owner; not done)

Two MIT-licensed converters were considered. Neither is adopted; this is the work
each would take.

| Candidate | Licence | Notes |
|---|---|---|
| `markdownify` | MIT | **Already in `uv.lock`** (transitively, via `markitdown` in the `rag` extra, together with `beautifulsoup4`, MIT). Emits `[text](url)` links like html2text does, so `mail/normalize.py` keeps working. Pure Python, small. |
| `html-to-markdown` | MIT | Maintained, faster (compiled core). Heavier to vendor in a PyInstaller bundle; output is closer to CommonMark and may differ in table and list handling. |

### Files that would change

| File | Change |
|---|---|
| `src/mailforge/mail/sanitize.py:18` | `import html2text` becomes `import markdownify` (or `html_to_markdown`). |
| `src/mailforge/mail/sanitize.py:54-60` | Replace `_make_html2text()` with a small wrapper that sets the equivalent options: no hard wrap (`body_width = 0`), ignore images, keep emphasis, keep Unicode, skip internal links. |
| `src/mailforge/mail/sanitize.py:84` | `_make_html2text().handle(cleaned)` becomes the new converter call. |
| `src/mailforge/mail/sanitize.py:1-12` (docstring) | The pipeline description names html2text. |
| `src/mailforge/mail/normalize.py:140` | Comment about "Markdown links emitted by html2text"; confirm the link syntax is unchanged and keep the link-symbolisation regex in step. |
| `pyproject.toml:26` | Replace `html2text>=2024.2.26` with the new dependency; promote `markdownify` from transitive to direct. |
| `uv.lock` | Regenerate (`uv lock`). |
| `THIRD_PARTY_NOTICES.md`, this file | Remove the copyleft section. |
| `tests/unit/` (sanitizer, normalize, screening fixtures) | Expect whitespace and list-marker differences; re-baseline the golden strings, and re-run the inbound-screening and untrusted-framing suites, because the screener scores the converted text. |

Because the converter output feeds injection screening and the LLM prompts, a
swap is a behaviour change to the security pipeline: do it as its own release
with the screening evaluation (`mailforge eval-screening`) re-run before and
after.
