# OpenClaw Email Agent

A **self-contained, cross-platform (Windows + Linux) local-LLM email assistant**.
It watches your inbox, classifies mail, and writes *draft* replies using a model
running on **your own machine** (LM Studio) — then waits for you to click
**Approve** before anything is ever sent.

> **It never sends mail on its own.** The agent only ever produces drafts.
> SMTP fires exclusively after an explicit human click in the local web UI.
> This is a hard architectural invariant, not a setting you can flip.

---

## Why it's safe by design

This is built to Meta's **"Agents Rule of Two"** class **[A]+[B]**: it handles
*untrusted input* (email) and *private data* (your mailbox) but performs **no
autonomous external state change**. Even if every detection layer were bypassed,
the worst case stays bounded — because the only thing the AI can do is *propose*.

- **No auto-send, ever.** `security.autosend_allowed` defaults `False` and the
  send path asserts a human-approval record exists.
- **No destructive AI tools.** The model never gets `send` / `delete` /
  `forward` / `archive`. It only gets `propose_draft` / `propose_label`.
- **Untrusted content is quarantined.** Raw email is sanitized → normalized →
  spotlight-wrapped before any model sees it; the planner never sees raw email.
- **Recipients are bound, not invented.** A draft can only go to someone already
  in the thread, your contacts, or the auto-built allowlist.
- **Secrets never hit disk in plaintext.** Credentials live in your OS keyring.
- **The database is `chmod 0600`. The web UI binds `127.0.0.1` only** with
  token→cookie auth and a Host-header check.
- **Output guardrails** (PII, secrets, malicious URLs, toxicity, cross-thread
  leak, recipient/flow policy) run before any draft is shown to you *and* again
  after you edit it.
- **Tamper-evident audit log** — every LLM call, tool call, approval, send, and
  block is recorded in a SHA-256 hash chain you can verify.

---

## Install & run (the easy path)

You need **two things**: this package, and **[LM Studio](https://lmstudio.ai)**
running locally with a chat model and the embedding model
`text-embedding-nomic-embed-text-v1.5` downloaded.

### Linux

```bash
# 1. install uv (the Python package runner) if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. install the app
uv tool install openclaw-email

# 3. (optional) install as a background service that survives reboot
openclaw-email service install

# 4. set up your account — walks you through it, stores creds in your keyring
openclaw-email setup-wizard

# 5. start it (if you skipped the service step)
openclaw-email serve
```

### Windows (PowerShell)

```powershell
irm https://astral.sh/uv/install.ps1 | iex
uv tool install openclaw-email
openclaw-email service install        # installs a Windows service via WinSW
openclaw-email setup-wizard
```

Then open the `http://127.0.0.1:<port>/?token=...` URL it prints. Review each
draft, **Approve / Edit / Reject**. Only **Approve** (or Edit→Approve) sends.

### Optional capability tiers

The base install is light and runs with built-in fallbacks. Add deeper models
as you want them:

```bash
uv tool install "openclaw-email[llm,rag]"          # LM Studio SDK + vector RAG
uv tool install "openclaw-email[security-ml]"      # Prompt Guard, Presidio, llm-guard, detect-secrets
uv tool install "openclaw-email[policy,redteam]"   # Invariant policy + garak/promptfoo CI
```

> Every security layer has a real regex/heuristic fallback, so the architecture
> stays safe even before you install the heavy ML models.

---

## Moving it to another computer

This is a normal Python package — it travels several ways:

1. **Re-install from the same source on the new machine** (recommended):
   `uv tool install openclaw-email` (or install from the built wheel, below).
   Your config (`~/.config/openclaw-email/config.toml`) and the mailbox DB
   (`~/.local/share/openclaw-email/agent.db`) are plain files you can copy over;
   **credentials are NOT in them** — re-run `setup-wizard` on the new machine to
   re-enter them into that machine's keyring.

2. **Build a wheel and carry it on a USB stick:**
   ```bash
   uv build                      # produces dist/openclaw_email-*.whl
   # on the other machine:
   uv tool install ./openclaw_email-0.1.0-py3-none-any.whl
   ```

3. **Windows, no Python at all on the target:** build the PyInstaller
   **folder-mode** bundle (`packaging/pyinstaller.spec`) and wrap it with the
   Inno Setup script (`installer/inno/openclaw-email.iss`). This produces a
   double-click installer. (Folder-mode, never one-file: faster cold start and
   fewer antivirus false positives. Code-sign it to remove the rest.)

Paths are resolved per-OS via `platformdirs`, so the same build behaves
correctly on Linux and Windows.

---

## Commands

| Command | What it does |
|---|---|
| `openclaw-email setup-wizard` | Interactive account/credential/config/DB setup |
| `openclaw-email serve` | Run the listener + agent + web UI |
| `openclaw-email serve --no-ui` | Headless (e.g. under the service) |
| `openclaw-email ingest` | Embed your style guides / context docs for RAG |
| `openclaw-email audit-verify` | Verify the audit hash chain is intact |
| `openclaw-email service install\|start\|stop` | Manage the background service |
| `openclaw-email redteam` | Run offline red-team suites (garak / promptfoo) |

---

## How a message flows

```
new mail (IMAP IDLE)
  → sanitize HTML → normalize unicode → strip invisibles → decode nested base64
  → symbolize links → spotlight-wrap (untrusted = data, never instructions)
  → injection score (telemetry only)
  → plan freeze (policy + symbolic facts only — never raw content)
  → classify (typed Category)
  → retrieve same-thread context
  → draft reply (recipient bound to the thread)
  → output guardrails (PII / secrets / URLs / toxicity / cross-thread / policy)
       PASS → PENDING  →  you Approve/Edit/Reject in the UI  →  SMTP send
       FAIL → BLOCKED  →  you're notified, nothing is sendable
  (LM Studio down? → DEFERRED_NO_LLM, resumes automatically — no mail lost)
```

## Architecture

See `src/openclaw_email/`:
`config.py`/`secrets.py` (settings + keyring), `db/` (schema + data layer),
`mail/` (IMAP/SMTP, sanitize/normalize), `llm/` (LM Studio bridge + structured
output), `rag/` (chunk/embed/retrieve), `agent/` (deterministic graph, planner,
quarantined worker, capability-restricted tools), `security/` (the guardrail
stack), `ui/` (NiceGUI approval app), `audit/` (hash-chain log + monitor),
`service/` (systemd + WinSW).

## License

MIT.
