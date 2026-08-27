# MailForge

A **self-contained, cross-platform (Linux + Windows) local-LLM email assistant**.
It watches your mailbox, screens and classifies what arrives, and writes *draft*
replies with a model running on **your own machine** — then waits for you to
click **Approve** before anything is ever sent.

> **It never sends mail on its own.** The agent only ever produces drafts.
> SMTP fires exclusively after an explicit human click in the local web UI.
> This is an architectural invariant, not a setting you can flip.

Try it in one command, with fictional data and no mailbox:

```bash
uv run mailforge demo
```

---

## Why it is safe by design

The design target is Meta's **"Agents Rule of Two"**, class **[A]+[B]**: the app
handles *untrusted input* (email) and *private data* (your mailbox) but performs
**no autonomous external state change**. Even if every detection layer were
bypassed, the worst case stays bounded — the only thing the model can do is
*propose*.

- **No auto-send, ever.** `security.autosend_allowed` defaults to `false`, and
  the send path asserts that a human-approval record exists.
- **No destructive AI tools.** The model never gets `send` / `delete` /
  `forward` / `archive`. It gets `propose_draft` / `propose_label`.
- **Untrusted content is contained.** Raw email is sanitized → normalized →
  spotlight-wrapped before any model sees it; the planner never sees raw email.
- **Hostile mail is QUARANTINED.** A message whose ingest-time injection score
  crosses `security.quarantine_threshold` is locked: no LLM can read it and the
  machine bridge returns metadata only, until a human releases it in the UI.
  Reading it yourself is always safe (plain text, no AI, links inert).
- **Public inboxes can be cynical.** A site may set
  `screening_mode = "content_only"`: mail related to the site's subject matter
  proceeds, unknown/off-topic mail becomes *Potential spam*, and account, login,
  payment, renewal, or legal claims become *Potential issue*. Those bodies stay
  human-only until reviewed. **Inbound links are never clickable** — open the
  provider's known site directly.
- **Spam learning is narrow and reversible.** "Spam & learn" applies to an exact
  sender or a stable multi-word subject pattern only. It never blocks a whole
  public domain, and labelling a message never deletes it — deleting is always a
  separate, deliberate action.
- **Deleting is two-stage, and you decide how far it goes.** Deleting puts a
  message in a local holding box. Spam and scam mail is then removed
  immediately; everything else is kept for `trash_retention_days` (60) so you
  can restore it, and only then destroyed. Whether the provider's copy goes too
  is yours to set: `server_delete_mode` is `trash` (server-side move into the
  account's Trash folder, recoverable there), `expunge`, or `off`.
- **Recipients are bound, not invented.** A draft can only go to someone already
  in the thread, in your contacts, or on the auto-built allowlist.
- **Secrets never hit disk in plaintext.** Credentials live in your OS keyring.
- **The database is `chmod 0600`; the web UI binds `127.0.0.1` only**, with
  one-shot-token→signed-cookie auth and a Host-header check.
- **Output guardrails** (PII, secrets, malicious URLs, toxicity, cross-thread
  leak, recipient/flow policy) run before a draft is shown to you *and* again
  after you edit it.
- **Tamper-evident audit log** — every model call, tool call, approval, send and
  block is recorded in a SHA-256 hash chain you can verify.

Every heavy security layer has a regex/heuristic fallback, so the architecture
stays safe before you install any of the optional ML models.

---

## Install & run

You need this package and a **local OpenAI-compatible LLM server**
([LM Studio](https://lmstudio.ai), llama.cpp's server, vLLM, Ollama's
OpenAI-compatible endpoint, …) serving a chat model and an embedding model.

### Linux

```bash
# 1. install uv (the Python package runner) if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. install the app (from a checkout or an unpacked release)
uv tool install .

# 3. look around first, with fictional data and no mailbox
mailforge demo

# 4. set up your real account — stores credentials in your OS keyring
mailforge setup-wizard

# 5. run it
mailforge serve

# 6. (optional) install as a background service that survives reboot
mailforge service install
```

### Windows (PowerShell)

```powershell
irm https://astral.sh/uv/install.ps1 | iex
uv tool install .
mailforge service install        # installs a Windows service via WinSW
mailforge setup-wizard
```

Then open the `http://127.0.0.1:<port>/?token=…` URL it prints — the token is
one-shot and upgrades to a signed session cookie. Review each draft and
**Approve / Edit / Reject**. Only Approve (or Edit → Approve) sends.

### Optional capability tiers

```bash
uv tool install ".[llm,rag]"          # local-LLM SDK + vector RAG
uv tool install ".[security-ml]"      # Prompt Guard, Presidio, llm-guard, detect-secrets
uv tool install ".[policy,redteam]"   # offline flow policy + garak/promptfoo CI
```

### Working from a checkout

```bash
uv run mailforge demo            # no install needed
uv run --extra dev pytest -q          # the test suite
uv run ruff check src tests scripts   # lint
```

---

## Commands

| Command | What it does |
|---|---|
| `mailforge demo` | Throwaway install seeded with fictional mail — nothing real is touched |
| `mailforge setup-wizard` | Interactive account / credential / config / DB setup |
| `mailforge account-add` | Add another mailbox without replacing existing configuration |
| `mailforge account-list` / `account-remove` | Inspect / detach mailboxes (history is kept) |
| `mailforge serve` | Run the listener + agent + web UI |
| `mailforge serve --no-ui` | Headless (e.g. under the service) |
| `mailforge open` | Open an authenticated browser session to the running UI |
| `mailforge ingest` | Embed your style guides / context docs for retrieval |
| `mailforge references-refresh` | Rebuild the managed per-site knowledge handbooks |
| `mailforge bridge <action> --site <id>` | Site-bound JSON stdin/stdout bridge for a local agent |
| `mailforge purge-trash` | Report the deletion queue; `--yes` runs the sweep now |
| `mailforge audit-verify` | Verify the audit hash chain is intact |
| `mailforge service install\|start\|stop\|desktop` | Manage the background service / launcher |
| `mailforge redteam` | Run offline red-team suites (garak / promptfoo) |

### Demo mode

`mailforge demo [--port N] [--no-browser] [--keep]` creates a temporary
application home (config + database) under your system temp directory, seeds ~25
fictional messages across two sites, and starts the normal UI against it. It
configures **no** IMAP account, so no mail is ever fetched or sent, and it never
opens your real config or database. The directory is deleted when you stop it.

---

## How a message flows

```
new mail (IMAP IDLE)
  → sanitize HTML → normalize unicode → strip invisibles → decode nested base64
  → symbolize links → save attachments (size-capped, disk-only, never to AI)
  → ingest injection score → QUARANTINE if >= threshold (no AI until released)
  → site screening → CONTENT, POTENTIAL SPAM, POTENTIAL ISSUE, or SPAM
       questionable/spam → sanitized human review only; no AI, no body over the bridge
       content/standard  → continue
  → spotlight-wrap (untrusted = data, never instructions)
  → plan freeze (policy + symbolic facts only — never raw content)
  → classify (typed category)
  → retrieve same-thread context
  → draft reply (recipient bound to the thread)
  → output guardrails (PII / secrets / URLs / toxicity / cross-thread / policy)
       PASS → PENDING  →  you Approve/Edit/Reject in the UI  →  SMTP send
       FAIL → BLOCKED  →  you are notified, nothing is sendable
  (LLM server down? → DEFERRED_NO_LLM, resumes automatically — no mail is lost)
```

---

## The approval UI

A localhost-only NiceGUI app in a dark slate + indigo system (design tokens and
component conventions live in **`docs/STYLE-GUIDE.md`**):

- **Dashboard** — the triage home: Received today / Unread / Needs reply / Needs
  action / AI drafts to review (plus Quarantined, Questionable and Filtered spam
  when non-zero), then *Needs a reply*, *Needs your action* and *Recent inbox*
  lists with bulk actions. Every chip and row is clickable.
- **AI Review** — state chips (Pending / Blocked / Deferred / Sent), tabs, live
  search over sender/subject/recipient/category, category and injection-risk
  badges, and red flags on external or first-time recipients. Updates push live
  over the WebSocket as mail arrives.
- **Draft detail** — chat-style review: the original email as an inbound bubble
  (links inert `[link_N]` symbols), thread provenance and guardrail flags as
  expansions, the editable draft as an outbound bubble, and the Approve /
  Edit-and-re-guardrail / Reject bar. A new external recipient must be re-typed
  exactly.
- **Mail + Compose** — a normal mail view with triage tabs, account filtering, a
  reading pane with Formatted (Markdown) / Plain text toggle, multi-select bulk
  Read / Unread / Archive / Delete / Restore (Delete is reversible until its
  retention period is up — see **Spam** below), an attachments card
  (download-only, never read by any AI), replies, persistent manual drafts,
  explicit send confirmation, and sent history. Replies always go out through
  the mailbox that received the original.
- **Spam** — everything the screener held back from the AI, in three boxes
  ordered by how likely a message is to be a scam (*Potential spam* → *Likely
  scam or phishing* → *Confirmed spam*), plus the **Scheduled for deletion**
  queue with a countdown per message. Select-all, shift-click ranges, an inline
  preview on row click, and bulk *Mark as spam & learn* / *Not spam* / *Delete*.
  A delete here skips the holding period and, unless `server_delete_mode` is
  `off`, removes the provider's copy too; if the mail server refuses or cannot
  be reached, the page says so and the next sweep retries.
- **AI response workshop** — a right-side chat drawer: ask for a first response,
  give feedback ("shorter", "warmer"), review revisions, apply one explicitly.
  Applying never sends, and the applied text is re-checked by the guardrails.
- **Reference Library** — upload TXT / Markdown / PDF / DOCX per site.
  Retrieval is bound to the receiving mailbox's persisted site, never to
  addresses or instructions inside an email.
- **Templates** — per-site response templates with a small, validated
  placeholder set (`{{first_name}}`, `{{site_name}}`, `{{signature}}`,
  `{{today}}`, `{{question}}`, `{{next_step}}`). A fresh database is seeded with
  a handful of neutral examples per site; edit or delete them freely — existing
  databases are never re-seeded.
- **Settings** — listener coverage per site (a site with no mailbox is flagged),
  security posture, and mailbox management. Credentials stay in the keyring and
  are never displayed.
- **Activity** — the chained-hash audit log, verified live on page load, plus
  the agent-notes log.
- **Models** — LLM server health and served models, with recovery guidance.

---

## Configuration

Everything lives in one TOML file:

| OS | Path |
|---|---|
| Linux | `~/.config/mailforge/config.toml` (data in `~/.local/share/mailforge/`) |
| Windows | `%APPDATA%\mailforge\config.toml` (data in `%LOCALAPPDATA%`) |

Set `MAILFORGE_HOME=/some/dir` to relocate both (`<dir>/config`,
`<dir>/data`) — that is how demo mode isolates itself. Any setting can also come
from the environment with the `MAILFORGE_` prefix and `__` nesting
(`MAILFORGE_SECURITY__QUARANTINE_THRESHOLD=0.9`). **Passwords are never
stored here** — they go to the OS keyring.

```toml
[[imap_accounts]]
name = "support"                 # unique label; also the keyring service name
site_id = "main"                 # must match a [sites.<id>] entry
host = "imap.example.com"
port = 993
username = "support@example.com"
auth_method = "password"         # or "xoauth2"
folders = ["INBOX"]
poll_interval_s = 30
# optional per-account SMTP override (mailbox on a different provider)
smtp_host = ""
smtp_port = 587
smtp_starttls = true

[smtp]                           # global relay used when an account has no override
host = "smtp.example.com"
port = 587
username = "support@example.com"
starttls = true

[sites.main]
name = "Northwind Studio"        # display name; fills {{site_name}}
guidance = "Do not invent prices, stock, availability, or delivery dates."
screening_mode = "standard"      # or "content_only" (see above)
triage_guidance = ""             # extra policy text shown to the triage prompt

[compose]
signature = "Alex"               # fills {{signature}} in templates and the composer

[llm]
lm_studio_host = "127.0.0.1:1234"   # any OpenAI-compatible server
chat_model = "your-chat-model"
embedding_model = "your-embedding-model"
ttl_seconds = 1800
context_length = 8192

[security]
require_human_approval = true    # invariant: must stay true
autosend_allowed = false         # invariant: must stay false
quarantine_enabled = true
quarantine_threshold = 0.85
prompt_guard_threshold = 0.85
recipient_allowlist_domains = []
recipient_block_domains = []
link_allowlist_domains = []
approvals_per_hour = 60
attachment_max_bytes = 15728640
attachment_max_count = 25
server_delete_mode = "trash"     # trash | expunge | off — what a delete does
                                 # to the copy on the mail server
trash_retention_days = 60        # how long deleted mail is kept before it is
                                 # destroyed (spam/scam is never held)

[style]
style_guide_paths = []           # documents embedded by `mailforge ingest`
context_doc_paths = []
tone = "professional"

[openclaw]                       # optional link to an external agent system
enabled = true
prompt_override_dir = ""         # default: <config dir>/prompts
heavy_lift_base_url = ""         # any OpenAI-compatible endpoint; "" => local only
heavy_lift_model = ""
heavy_lift_key_env = "MAILFORGE_HEAVY_KEY"
agent_send_enabled = false       # prompt-gated agent-relayed send; ships OFF
agent_send_per_hour = 10
agent_send_token_ttl_s = 900
```

`db_path` may be set at the top level to move the SQLite database.

### Sites

A *site* is one brand/business. Every mailbox belongs to exactly one, and
retrieval, templates and the bridge are all site-scoped. Adding a site is
config-only — the database migrates itself on the next start:

```toml
[sites.shop]
name = "Acme Workshop"
guidance = "Never invent prices, stock, lead times, or delivery dates."
screening_mode = "content_only"
```

Sites also carry the knowledge-handbook keys used by
`mailforge references-refresh`, which builds one canonical, indexed
handbook per site from your own source material:

| Key | Meaning |
|---|---|
| `knowledge_root` | Directory of source pages the handbook is built from |
| `knowledge_pages` | Explicit list of pages to include (overrides discovery) |
| `knowledge_required_pages` | Pages that must be present, else the build reports an error |
| `knowledge_exclude_patterns` | Glob/regex patterns to leave out (drafts, private notes) |
| `knowledge_intro` | Optional sentence introducing the full-text section |
| `policy_file` | A policy/rules document merged into the handbook |
| `site_url` | Public site URL used for links in the handbook |
| `openclaw_workspace` | Folder that also receives a copy of `SITE-HANDBOOK.md` |

### The optional agent link

The app is a stand-alone client — with no external agent system present it is
fully functional on your local LLM server. When one is present:

- **Prompt updates** — drop updated `<name>.txt` templates (`system_planner`,
  `system_worker`, `classify`, `draft`, `revise`) into the prompt-override dir
  (`<config dir>/prompts/`, or `MAILFORGE_PROMPT_DIR`); they win over the
  packaged defaults with no restart. `openclaw.enabled = false` ignores them.
- **Heavy lifting when online** — point `heavy_lift_base_url` at any
  OpenAI-compatible endpoint; when set *and reachable* the draft step uses it,
  and on any error it falls back to the local model.
- **The bridge** — `mailforge bridge <action> --site <id>` speaks JSON on
  stdin/stdout: `status`, `list`, `get`, `propose`, `revise`, `daily-brief`,
  `weekly-summary`, `reference-search`, `notes`, `note-add`. Quarantined and
  screened-out messages are **metadata only**; eligible bodies come wrapped in
  explicit untrusted-content markers with a content-policy field. There is no
  delete, archive, move, forward, credential, filesystem or shell tool.
- **Human-instructed send** — OFF by default (`openclaw.agent_send_enabled`).
  When enabled, `prepare-send` re-runs the guardrails, binds the recipient to
  the thread/allowlist (first-contact addresses are refused — use Compose) and
  returns the exact message plus a single-use token (15 min TTL, voided by any
  edit, rate-capped). Only after a human confirms that content does `send`
  transmit, fully audited. Autonomous sending remains impossible.

---

## Architecture

`src/mailforge/`: `config.py`/`secrets.py` (settings + keyring), `db/`
(schema + the only place SQL lives), `mail/` (IMAP/SMTP, sanitize, normalize),
`llm/` (bridge + structured output), `rag/` (chunk/embed/retrieve), `agent/`
(deterministic graph, planner, quarantined worker, restricted tools),
`security/` (the guardrail stack), `ui/` (NiceGUI app), `audit/` (hash-chain log
+ anomaly monitor), `service/` (systemd + WinSW), `demo.py` (demo dataset).

Paths resolve per-OS through `platformdirs`, so the same build behaves correctly
on Linux and Windows. `packaging/pyinstaller.spec` (folder mode, never one-file)
plus `installer/inno/mailforge.iss` produce a double-click Windows
installer for machines with no Python.

---

## Roadmap / known limits

- **UIDVALIDITY is not tracked.** If the server renumbers UIDs, re-ingestion can
  duplicate or skip messages; delete the account's rows and re-backfill.
- **Only the first configured folder is watched** per account (`folders[0]`).
- **Reply-all, forward and Cc are not implemented** — replies go to the sender
  through the receiving mailbox.
- **Agent-relayed send is a prompt-gated opt-in that ships OFF**, and always
  requires an explicit human confirmation round-trip.
- Attachments are stored and downloadable but are never read by any model.
- Threading is heuristic (`In-Reply-To`/`References`, then normalized subject).

---

## License

MIT — see [`LICENSE`](LICENSE).

Published by Laser Lloyd — https://www.laserlloyd.com
