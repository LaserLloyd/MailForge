# MailForge — UI Style Guide

Canonical style guide for the NiceGUI frontend in `src/mailforge/ui/`.
Colours come from the shared theme folder `src/mailforge/ui/static/ui-theme/`
(see §2); layout and component CSS live in `src/mailforge/ui/theme.py` —
this document explains how to use both.
If code and this guide disagree, fix whichever one is wrong.

---

## 1. Principles

- **Local-first, always.** This app runs fully offline. No CDN scripts, no
  remote fonts, no remote images, no analytics. `--font-sans`/`--font-mono`
  are stacks with system fallbacks (`'Inter', -apple-system,
  BlinkMacSystemFont, 'Segoe UI', sans-serif` / `'JetBrains Mono', 'Fira
  Code', ui-monospace, monospace`) — if those fonts aren't installed
  locally, the browser falls back silently. Never add a `@font-face` that
  points at a URL.
- **Themes are picked, not coded.** The user picks a theme in the sidebar
  (dark and light ones); Quasar's dark mode follows it. Never assume a dark
  background.
- **One accent color.** `--accent` (whatever the theme makes it) is the only
  brand hue. Status colours are reserved for state only — never
  decorative. Reuse `--accent` for new features instead of adding a color.
- **Flat cards, 1px borders.** `.bb-card` uses `box-shadow: none`; depth
  comes from a 1px `var(--border)` line plus `var(--surface-2)` on
  `var(--surface-0)`, not shadows or gradients.
- **Security states are always visually loud.** Quarantine and
  blocked/rejected states use the danger (`--danger*`) family with an icon
  (`gpp_maybe`) — never a quiet gray badge.
- **Human-approval messaging stays visible.** `theme.shell()` renders
  "human approval — nothing sends without you" in the header on every page
  (hidden only below the 760px breakpoint, where space is the constraint).
  Any new send/approve flow must keep restating what the AI can and
  cannot do (§8).

---

## 2. Themes and design tokens

MailForge does not define colours. They come from the **shared theme
folder** `src/mailforge/ui/static/ui-theme/`, the **ForgeUI** (ThemeForge) drop-in
bundle every app uses: the theme runtime, the tokens for every theme, the
NiceGUI/Quasar adapter, the component layer, and `VERSION` (`ThemeForge <version>
<digest>`) plus `files.json` (a sha256 for every file). **Never edit a file in that
folder.** The folder is written whole by `python -m forgeui vendor`, driven by the
manifest `forgeui.json` at the repository root (it names the folder, the accepted
version `1.2`, the adapter `quasar`, and the app's theme settings). Nothing else
changes: the folder is served at `/mf-theme/` with `no-cache`, so a new copy is live on
the next page load.

To update the bundle (no private registry, no copy-paste):

```bash
uv pip install "forgeui @ git+https://github.com/LaserLloyd/ThemeForge@v1.2.0"   # the release forgeui.json accepts (not PyPI)  # scrub-check: allow (public repo URL)
python -m forgeui vendor            # replace the folder, verified against files.json, VERSION last
python -m forgeui vendor --check    # exit 1 on drift (CI and tests/unit/test_forgeui_vendor.py run this)
# offline: python -m forgeui vendor --source <ThemeForge checkout, ui-theme folder, zip or wheel>
```

`forgeui` is a development tool, not a dependency of MailForge. A hand-edited bundle file
is refused unless you pass `--force`; MailForge's own CSS belongs in `theme.py`, not in the
bundle (an app-owned overlay file would go under `extra_files` in `forgeui.json`, never
appended into a bundle file).

MailForge's own settings are in **one place**, `forgeui.json` `settings`: the picker's
themes and order (the six original core themes, the new Slate, High Contrast and High
Contrast Light, then Night Red), the default for a new browser (`midnight-gold`) and the
storage key (`mailforge.theme`). `theme.THEME_SETTINGS` reads them and writes them onto the
runtime's `<script>` tag. The package carries a byte copy of the manifest,
`src/mailforge/ui/forgeui.json` (the wheel, the container image and the frozen build have
no repository root); after editing `forgeui.json`, run
`cp forgeui.json src/mailforge/ui/forgeui.json` (a test fails if they differ). A theme added
to the list needs a service restart. Opt-in themes (`electric-yellow`, `laserlloyd*`) are <!-- scrub-check: allow (public theme id) -->
offered only if listed there.

Component CSS in `theme.py` and inline styles in page files read **contract
tokens only**, never a literal colour:

| Token | Usage |
|---|---|
| `--surface-0` | Page/body background, full-window reader background. |
| `--surface-1` | Header, sidebar drawer, AI drawer, reader sticky header, metric cells. |
| `--surface-2` | Cards (`.bb-card`), rows (`.bb-row`), stat chips, sync chip. |
| `--surface-3` | Hover state for nav buttons/rows; badge default background. |
| `--surface-sunken` | Quasar outlined-field background, theme picker. |
| `--text-primary` / `--text-secondary` / `--text-tertiary` | Body copy / metadata and labels / timestamps and muted icons. Make text quieter with a lower tier, never with opacity. |
| `--text-link` | Links, accent-tinted text and icons. |
| `--accent`, `--accent-subtle` | Active-nav and selected-row borders, `color=primary` / their wash backgrounds. |
| `--border`, `--border-strong` | Default 1px border / hover border, badge border, dividers. |
| `--bubble-user-*`, `--bubble-assistant-*` | Chat bubbles (bg, text, border). |
| `--success*`, `--warning*`, `--danger*`, `--info*` | State colours: the bare token is a fill, `-text` is for text and icons, `-subtle` a background, `-border` an edge. |
| `--radius-sm` / `--radius-md` / `--radius-lg` | Radii; `--radius-md` is the standard. |
| `--font-sans` / `--font-mono` | UI text / code blocks, `.bb-mono`, `<code>`/`<pre>`. |

Pages that pass a status tone by name (`"warning"`, `"error"`, `"success"`,
`"accent-hover"`, ...) resolve it with `theme.tone_text(tone)` for text or an
icon and `theme.tone_border(tone)` for an edge, so light themes stay readable.

---

## 3. Typography

Two stacks, no others: `var(--font-sans)` (Inter) for chrome/prose,
`var(--font-mono)` (JetBrains Mono) for anything that reads as *data* —
guardrail-flag JSON, provenance/thread rows (`.bb-mono` in `detail.py`,
`activity.py`), raw audit fields. There's no type-scale variable; sizes are
inline `.style("font-size: …")` — match these rather than inventing new
ones:

| Size | Weight | Where |
|---|---|---|
| `18px` | `800` | Full-window reader title, and `mail.py`'s inline message-reader subject (`subject_label(..., full=True)`). |
| `1.25rem` (20px) | `700` | Draft-detail page header subject — one step up since it's the page's single most important line. |
| `16px`–`17px` | `700`–`800` | Dashboard card-section titles ("Needs a reply", etc.), empty-state headlines. |
| `13px`–`13.5px` | `400`–`700` | Body: list-row subjects (`.bb-subject`), message body (`markdown.plain_text`), row sender names. |
| `12px`–`12.5px` | `400`–`600` | Secondary descriptive text: reader notes, provenance rows, sender/time lines. |
| `11px`–`11.5px` | `400`–`600` | Metadata: snippets, `.bb-badge` text (`11.5px`), stat-chip labels. |
| `10.5px` | `400` | Smallest text in the system: row timestamps in the dense mail list. |

Never go below 10.5px, and keep sub-12px sizes for timestamps/badges/
snippets only — never a subject or primary label.

---

## 4. Layout

### `theme.shell()`

Every page wraps its content in `theme.shell(active, title, bridge=None,
store=None, max_width=1280)`:

```python
with theme.shell("mail", title="Inbox", bridge=bridge, store=store):
    ...page content...
```

It applies the palette, builds the left drawer (from the module-level `NAV`
list), builds the header (title, the permanent approval note, a live health
dot when `bridge` is passed), then yields a centered content column
(`bb-main-column`) clamped to `max(760, max_width)`px with `gap: 14px`.
Page code only fills that column.

### Sidebar

`ui.left_drawer(value=None, fixed=True).props(":breakpoint=760 :width=210")`
— Quasar's `:breakpoint` collapses it to an overlay below 760px, toggled by
the hamburger (`.bb-menu-btn`, hidden above 760px). Add pages to `NAV` in
`theme.py`, not by hand-building a nav button in a page file.

### Content column and card grid

Cards use a flex-wrap row with `flex: 1 1 400px` so 1–3 columns reflow as
the window narrows:

```python
with ui.row().classes("w-full items-stretch").style("gap: 14px; flex-wrap: wrap"):
    with ui.card().classes("bb-card").style("padding: 14px; flex: 1 1 400px; min-width: 0"):
        ...
```

Always pair `flex: 1 1 400px` with `min-width: 0` — otherwise long
unbroken text (an address, a subject) can force the item wider than its
basis and break the wrap.

### Mail workspace (split pane)

`.bb-mail-workspace` is a CSS grid (`minmax(310px, 390px) minmax(0, 1fr)`)
holding `.bb-mail-list` (scrolling rows) and `.bb-mail-pane` (scrolling
reader), each scrolling to `calc(100vh - 160px)`.

### 760px breakpoint

One `@media (max-width: 760px)` block does all responsive work: the drawer
becomes a slide-over (hamburger appears, header note hides), `.bb-toolbar`
rows wrap/stack, stat chips shrink their min-width. Most notably, **the mail
workspace collapses from two panes to one**: `mail.py` adds a
`.has-selection` class to the workspace div whenever a `message_id` is
selected, and CSS toggles visibility —
`.has-selection .bb-mail-list { display: none }`,
`:not(.has-selection) .bb-mail-pane { display: none }`. No selection shows
the list; a selection shows the reader full-width with a "Back to inbox"
button. Follow this pattern for any new split-pane view.

---

## 5. Components

**`.bb-card`** — base surface (`var(--surface-2)`, 1px border, 12px
radius, no shadow). Use for any self-contained block.

```python
with ui.card().classes("w-full bb-card").style("padding: 12px; gap: 6px"):
    ui.label("Attachments (2)").style("font-weight: 700")
```

**`.bb-row`** — clickable list row (inbox rows, dashboard mini-rows). Same
surface as a card plus pointer cursor and hover (`--surface-3` /
`--border-light`). Add `.bb-row--selected` for the open row (accent border
+ tinted background). Build on `ui.element("div")`, not `ui.card()` — cards
carry Quasar shadow/ripple assumptions that fight the flat style.

```python
with ui.element("div").classes("bb-row w-full").on(
    "click", lambda _e, i=message_id: ui.navigate.to(f"/mail?message_id={i}")
):
    ...
```

**`.bb-stat`** — headline-number chip: big number (`.bb-stat-n`, 22px/700)
over an uppercase label (`.bb-stat-l`, 11.5px). Use for a dashboard-style
counter. Click affordance is manual — there's no built-in link variant.

```python
chip = ui.element("div").classes("bb-stat").style("cursor: pointer").on(
    "click", lambda: ui.navigate.to("/mail?filter=unread")
)
with chip:
    ui.label(str(unread_n)).classes("bb-stat-n").style("color: var(--accent-hover)")
    ui.label("Unread").classes("bb-stat-l")
```

**`.bb-badge`** (+ `theme.badge()`, `theme.state_badge()`,
`theme.risk_badge()`, `theme.quarantine_badge()`,
`theme.screening_badge()`) — never hand-write a badge
span. `kind` is one of `""` / `"accent"` / `"success"` / `"error"` /
`"warning"`, each an `.bb-badge--{kind}` modifier tinting bg+border+text
together.

```python
theme.badge("shop", "accent")
theme.state_badge(draft["state"])   # PENDING/APPROVED/SENT/... -> colored pill
theme.risk_badge(injection_score)   # "risk 0.42" green/amber/red, or None
theme.quarantine_badge()            # always "QUARANTINED", error kind
theme.screening_badge(status)        # content / questionable / spam state
```

`risk_badge` returns `None` (renders nothing) for a missing score — no
placeholder badge, no `if` guard needed at the call site.

**`.bb-bubble`** — chat-style bubbles on the draft-detail page for the
original-email/drafted-reply pair. `--bot` (inbound) squares the
bottom-left corner; `--user` (outbound) squares the bottom-right and adds
an accent border tint.

```python
with ui.element("div").classes("bb-bubble bb-bubble--bot w-full"):
    markdown_ui.plain_text(sanitized_body)
```

The AI assistant drawer (`assistant.py`) uses a **separate, parallel**
pair — `.bb-chat-message` / `--user` / `--assistant` (adds
`white-space: pre-wrap` for streaming text) — for its live chat transcript
inside `.bb-chat-scroll`. Use `.bb-bubble` for the persistent
original/draft pair on a page; use `.bb-chat-message` only inside the AI
drawer's transcript.

**`.bb-sticky-actions`** — pins a page's primary action row to the
viewport bottom with a translucent-dark backdrop (`rgba(16,24,48,.96)`) so
scrolled content doesn't show through.

```python
with ui.row().classes("w-full justify-end bb-toolbar bb-sticky-actions").style("gap: 10px"):
    reject_btn = ui.button("Reject", icon="close", on_click=_reject).props("outline color=negative")
    approve_btn = ui.button("Approve & Send", icon="send", on_click=_approve_and_send).props(
        "unelevated no-caps color=positive"
    )
```

**Health dot** — `.bb-dot`, an 11px circle in the header: gray/idle
default, `.bb-dot--up`/`--down` (glow) once `_health_dot()`'s async probe
resolves. It's private to `theme.py` — pages get it for free by passing
`bridge`/`store` into `shell()`, never by calling `_health_dot()` directly:

```python
with theme.shell("mail", title="Inbox", bridge=bridge, store=store):
    ...
```

Its tooltip carries the full status (`"AI drafting online (model-id);
references: vector search ready"` / `"AI drafting OFFLINE — drafts
deferred; …"`) since the dot alone can't say that much.

---

## 6. Subjects (IMPORTANT)

**Email subjects are never single-line-ellipsized.** A truncated subject
can hide the one word that changes an email's meaning ("URGENT", "NOT", a
reference number). Always render subjects through `theme.subject_label()`
or `theme.clean_subject()` — never a raw `ui.label` with
`nowrap`/ellipsis.

```python
def subject_label(subject: Any, *, full: bool = False, style: str = "") -> ui.label:
    """Render an email subject that is never silently truncated.

    Lists get a 2-line clamp with the full subject in a tooltip; readers
    (``full=True``) wrap without limit.
    """
```

- **Lists** (`subject_label(subject)`, `full=False`, default) — applies
  `.bb-subject` (`-webkit-line-clamp: 2`, `overflow: hidden`) and attaches
  a `ui.tooltip(text)` with the complete subject. Nothing is lost, just
  deferred to hover.
- **Readers** (`subject_label(subject, full=True)`) — applies
  `.bb-subject--full` (`white-space: normal`, no clamp): the subject wraps
  onto as many lines as it needs. Used for the full-window reader title and
  the draft-detail header.
- **`clean_subject(subject)`** is the string-only half: unfolds header line
  breaks, collapses whitespace, returns `"(no subject)"` for empty input.
  `subject_label` calls it internally — call it directly only when you need
  the plain string (e.g. building a reply subject).

| Class | Behavior | Use for |
|---|---|---|
| `.bb-subject` | 2-line clamp, wraps, `overflow-wrap: anywhere` | List-row subjects (paired with a tooltip via `subject_label`) |
| `.bb-subject--full` | No clamp, wraps freely | Reader/detail-page subjects (`subject_label(..., full=True)`) |
| `.bb-clip-1` | Single line, ellipsis | **Senders and snippets only** — from-name, body snippets, filenames, "→ recipient" lines. **Never a subject.** |

If a tight layout tempts you to write
`ui.label(subject).classes("bb-clip-1")` — that's the bug this section
exists to prevent. Use `theme.subject_label()` instead.

---

## 7. State colors

`theme.state_badge()` is the one place the draft-state → color mapping
lives:

```python
kinds = {
    "PENDING": "accent", "APPROVED": "success", "SENT": "success",
    "REJECTED": "error", "BLOCKED": "error",
    "DEFERRED_NO_LLM": "warning", "DRAFT": "",
}
```

(`DEFERRED_NO_LLM` displays as "DEFERRED".) Pending work is accent
("needs you"); anything sent or explicitly approved is `--success`;
anything that cannot proceed (rejected, or blocked by a guardrail) is
`--danger`; anything waiting on an unavailable resource is `--warning`. `DRAFT` (untouched) gets the neutral badge — it hasn't earned a
color yet.

### Quarantine

Always the error family, always paired with the `gpp_maybe` icon and an
explanation — never a silent badge:

```python
with ui.card().classes("w-full bb-card").style("padding: 12px; border-color: var(--danger-border)"):
    ui.icon("gpp_maybe", size="22px").style("color: var(--danger-text)")
    ui.label("Quarantined — suspected prompt injection").style(
        "font-weight: 800; color: var(--danger-text)"
    )
```

`theme.quarantine_badge()` (`"QUARANTINED"`, error kind) is the compact
list-row form; the full card treatment (reason + "Release" button) belongs
in the message reader.

### Injection-risk thresholds

`theme.risk_badge(risk)` renders `"risk {v:.2f}"`, bucketed inline:

| Score | Kind | Color |
|---|---|---|
| `< 0.5` | `success` | green |
| `0.5 – 0.85` | `warning` | amber |
| `>= 0.85` | `error` | red |

`None`/non-numeric returns `None` (nothing rendered) — don't add a
placeholder badge for a missing score; its absence already communicates
"not scored."

### Inbound screening and links

Potential spam uses the warning family; Potential issue and Spam use the
error family. In the full reader each gets an icon, reason, explicit statement
that no AI can read it, and alignment actions. `CONTENT` is success-colored.

Inbound email must call `markdown_ui.reader(..., allow_links=False)`. Do not
resolve `[link_N]` targets or create an anchor in any inbound-mail view. Always
show the direct-site instruction: for login, account, payment, renewal, or
security actions, the user opens the provider's known site independently.

---

## 8. Voice & microcopy

- **Sentence case** for body copy and headings (`"Nothing is waiting on a
  response."`, `"Released from quarantine."`). The exceptions are short
  imperative buttons.
- **Buttons are verbs**: `"Approve & Send"`, `"Release"`, `"Reject"`,
  `"Save Changes"`, `"Compose"`, `"Reply"`, `"AI draft"` / `"AI Revise"`.
- **Security copy is short, factual, and names the mechanism**, not just
  the conclusion:
  - `"No AI (local model or OpenClaw) can read this message until you
    release it. Reading it here is safe."`
  - `"Links shown as RESOLVED targets (you see real URLs; the LLM never
    did)."`
  - `"Sanitized content. Remote images, scripts, and active content are
    never loaded."`
  - `"Attachments are stored locally and are never read by any AI. Open
    them only if you trust the sender."`
- **Always state what the AI can/cannot do.** The permanent header note
  (`"human approval — nothing sends without you"`) is the anchor; the
  dashboard's "how it works" panel spells out the pipeline (`"AI prepares a
  draft; it **cannot send**. You review, revise..., and explicitly
  approve."`), and success states echo it (`"Draft saved; guardrails
  passed. Nothing was sent."`).
- **Errors state the resulting state, not just failure**:
  `"Send failed after approval: {e}. The draft is back in Pending for
  review/retry."` — never leave the user unsure whether something sent.
- **Pluralize numbers where cheap**:
  `f"Review {pending} AI draft{'s' if pending != 1 else ''}"`.

---

## 9. Accessibility

- **Contrast comes from the theme, not from this app.** Every theme's text
  tiers are contrast-checked on its surfaces by the theme repository, so use
  the tiers as meant: `--text-secondary` on `--surface-2` is the standard
  body/card pairing; `--text-tertiary` is for genuinely secondary decoration
  only (timestamps, muted icons, disabled affordances) — never the sole copy
  of an important fact. State text uses the `-text` tier (`theme.tone_text`).
- **Tooltips carry the full content whenever something is clamped.** This
  is load-bearing for `.bb-subject`'s 2-line clamp: `subject_label()`
  always attaches a `ui.tooltip(text)` with the untruncated subject in list
  form. Give any new clamped/truncated element the same treatment —
  clamping is a layout affordance, not a way to drop information. The
  health dot follows the same idea: the visual state is binary, but its
  tooltip always carries the full status sentence.
- **Icon-only buttons always get a `.tooltip(...)`** (e.g.
  `ui.button(icon="download", ...)`, `ui.button(icon="mark_email_unread",
  ...)`) — never ship an icon-only control without a hover/focus label.
- **Keyboard/focus rides on Quasar's defaults** — buttons, inputs, tabs,
  the drawer toggle keep native focus rings and `Enter`/`Space`
  activation; don't add `outline: none` when reskinning a component. The
  one manual keyboard binding is search's `Enter`-to-search
  (`search.on("keydown.enter", ...)` in `mail.py`) — mirror that pattern
  rather than relying on implicit form submission (NiceGUI pages aren't
  real `<form>` elements).
- **Color is never the only signal.** `state_badge` always renders the
  state name as text alongside its color; quarantine always pairs red with
  the `gpp_maybe` icon and a sentence; unread mail pairs the accent color
  with an icon change (`mark_email_unread` vs `mail`, or a filled vs
  transparent `circle` in `mail.py` rows).
