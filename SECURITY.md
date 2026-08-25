# Security policy

BlueBox reads a real mailbox, holds mailbox credentials in your OS
keyring, and lets a local language model draft replies. Its security properties
are the product, so please report anything that weakens them.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** — the *Security* tab →
*Report a vulnerability*. That opens a private channel visible only to the
maintainers; please use it rather than a public issue for anything exploitable.

Expect an acknowledgement within a week. If a report is valid I will confirm the
affected versions, prepare a fix, and credit you in the changelog unless you ask
me not to.

**Never include real credentials, message bodies, or mailbox contents in a
report.** A redacted reproduction is always enough; if it genuinely is not,
say so and we will work out how to share it safely.

## What counts

These are the invariants. A way to break any of them is a vulnerability, not a
bug report:

- **Nothing sends without a human.** `security.autosend_allowed` is false and
  `security.require_human_approval` is true, enforced at startup by
  `Settings.assert_invariants()`. Any path that puts mail on the wire without an
  explicit human approval is the most serious report you can make.
- **A quarantined or unscreened body never reaches a model.** Messages held by
  injection quarantine or by site screening (`POTENTIAL_SPAM`,
  `POTENTIAL_ISSUE`, `SPAM`) are human-only; neither the local LLM nor the
  machine bridge may see their content until a human releases them.
- **Recipients are bound, not invented.** A draft may only address someone
  already in the thread, in the allowlist, or re-typed by hand.
- **The UI is local-only.** It binds `127.0.0.1` on a persisted random high
  port, rejects any request whose `Host` header is not that address (including
  WebSocket upgrades and their `Origin`), and upgrades a one-shot launch token
  to a signed session cookie. Anything reachable off-box, or any way past that
  gate, is in scope.
- **Inbound links are inert.** Real URLs stay controller-side as `[link_N]`
  symbols and are never rendered as clickable links or handed to a model.
- **Secrets stay in the keyring.** Mailbox passwords and OAuth tokens live in
  the OS keyring; config and database must never contain them. A path that
  writes a credential to disk, a log, or a model prompt is in scope.
- **Deleting is bounded.** Provider-side deletion may only ever target the
  `(folder, uid)` pairs this app recorded at ingest — never a search, never a
  whole folder. A way to make it delete anything else is in scope.
- **The audit chain is append-only.** `bluebox audit-verify` must detect
  tampering; a way to rewrite history undetected is in scope.

## What does not count

- Findings that require someone to already be logged in as you on your own
  machine. The trust boundary is the local user account.
- The optional heavy tiers (`security-ml`, `policy`) being absent. Those degrade
  to documented heuristic fallbacks by design; weaker detection is expected, and
  the fallbacks themselves are in scope.
- Spam or phishing that the screener misclassifies. That is an accuracy issue —
  file it as an ordinary issue with a redacted example.

## Supported versions

The latest released version. This is a small project; there are no backported
security branches.
