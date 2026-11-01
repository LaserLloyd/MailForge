# Licensing notes

This is an engineering note for the maintainer, not legal advice.

- MailForge is licensed MIT No Attribution (MIT-0, `LICENSE`).
- Through 0.7.0 the core dependency `html2text` (GPL-3.0-or-later) sat in the HTML-to-text step,
  which mattered for the PyInstaller bundle and the Inno installer (they put it in the same
  process as MailForge's code). **0.7.1 replaced it with `markdownify` (MIT)**
  (`src/mailforge/mail/sanitize.py`), so no distribution channel carries a GPL runtime library.
- `pyinstaller` (GPL-2.0-or-later with the bootloader exception) is a build-time tool only; the
  exception lets the produced executables be distributed under any licence.
- Keep `THIRD_PARTY_NOTICES.md` current; generate a full per-package licence list from `uv.lock`
  when an SBOM is built.
- The converter feeds injection screening and the LLM prompts, so a future swap should re-run
  `mailforge eval-screening` before and after.
