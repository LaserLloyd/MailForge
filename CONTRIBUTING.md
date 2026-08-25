# Contributing

Thanks for looking. This is a security-sensitive app — it holds mailbox
credentials and reads real mail — so the bar for a change is a little different
from a normal project. It is not high, it is just specific.

## Setup

```bash
# uv runs everything; no manual virtualenv needed
curl -LsSf https://astral.sh/uv/install.sh | sh    # or: winget install astral-sh.uv

git clone <your fork>
cd emailforge
uv sync --extra dev

uv run pytest -q          # the suite: fast, offline, no mailbox needed
uv run ruff check .       # lint (CI runs exactly these two)
```

Never point a work-in-progress at a real mailbox. `uv run emailforge demo`
builds a throwaway application home, seeds fictional mail, configures no IMAP
account, and deletes itself on exit — develop against that.

## What a change needs

1. **A test that fails without it.** The suite is fast and offline on purpose;
   there is no excuse for an untested behaviour change.
2. **The invariants intact.** `SECURITY.md` lists them. If your change touches
   sending, screening, quarantine, the local-only UI gate, credential handling,
   or provider-side deletion, say in the PR which invariant you considered and
   why it still holds.
3. **Lint clean.** `uv run ruff check .`. The codebase is not `ruff format`-ed:
   several files are hand-laid-out for readability, so please match the
   surrounding style rather than reformatting a file you are editing.

## Things worth knowing before you start

- **The optional extras are optional.** `security-ml`, `llm`, `rag` and
  `policy` each degrade to a documented fallback, and CI runs *without* them.
  A change that only works with the heavy stack installed is a regression.
- **Nothing may send mail without a human.** There is no "just for testing"
  exception to this; it is asserted at startup.
- **Bodies are dangerous input.** Anything that arrives in a message — subject,
  body, header, attachment name — is attacker-controlled. It is sanitized before
  storage and screened before any model sees it; keep new paths on that side of
  the line.
- **Comments explain *why*.** The code is dense with rationale for decisions
  that look arbitrary until you know the failure they came from. When you make a
  non-obvious call, leave the reason, not a restatement of the code.

## Reporting

Bugs and feature ideas: open an issue. Anything exploitable: **do not** open a
public issue — see `SECURITY.md` for private reporting.
