"""Demo mode — a throwaway install seeded with fictional mail.

``emailforge demo`` builds a temporary application home (config + data +
SQLite), seeds it with the messages below, and starts the normal UI against it.
Nothing here touches a real mailbox: there are no IMAP accounts configured, so
no listener runs, and every address is under an ``example.*`` domain reserved
for documentation (RFC 2606).

The seeding is a plain function of a :class:`~emailforge.db.store.Store`
so tests can call :func:`seed_demo` directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Sites the demo database is built around.
DEMO_SITES: dict[str, str] = {"main": "Northwind Studio", "shop": "Acme Workshop"}

#: config.toml written into the demo home. No IMAP accounts on purpose: the
#: listener list stays empty, so the demo never talks to a mail server. The LLM
#: host points at the usual local default; when nothing is served there the
#: bridge probe simply fails and drafts stay DEFERRED_NO_LLM.
DEMO_CONFIG_TOML = """# emailforge demo configuration (throwaway).
# Two fictional mailboxes so Compose/Reply have a From address to offer. No
# credentials exist for them and the listeners are disabled in demo mode, so
# nothing is ever fetched or sent (a send attempt stops at "no SMTP secret").
[[imap_accounts]]
name = "main"
site_id = "main"
host = "imap.example.com"
username = "hello@example.com"
auth_method = "password"
smtp_host = "smtp.example.com"

[[imap_accounts]]
name = "shop"
site_id = "shop"
host = "imap.example.net"
username = "orders@example.net"
auth_method = "password"
smtp_host = "smtp.example.net"

[sites.main]
name = "Northwind Studio"
guidance = "Do not invent business facts; if context is missing, say so."

[sites.shop]
name = "Acme Workshop"
guidance = "Never invent prices, stock, lead times, or delivery dates."
screening_mode = "content_only"

[compose]
signature = "Alex Example"

[llm]
lm_studio_host = "127.0.0.1:1234"
chat_model = "your-chat-model"
embedding_model = "your-embedding-model"

[security]
require_human_approval = true
autosend_allowed = false
"""


def _iso(days_ago: float) -> str:
    """UTC ISO timestamp ``days_ago`` days before now."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# (site, folder, from_addr, from_name, subject, body, days_ago, seen,
#  has_attachments, links, screening_status, quarantined)
_MESSAGES: tuple[tuple[Any, ...], ...] = (
    (
        "main", "alex.rivera@example.com", "Alex Rivera",
        "Question about your workshop guide",
        "Hi — I read your article on jig alignment and have a question about step 4. "
        "Does the clamp position change for thicker stock?",
        0.2, 0, 0, 0, "CONTENT", 0,
    ),
    (
        "main", "sam.rivera@example.net", "Sam Rivera",
        "Re: Question about your workshop guide",
        "Thanks, that worked. One more: which bit did you use for the final pass?",
        0.1, 0, 0, 0, "CONTENT", 0,
    ),
    (
        "main", "jordan.pike@example.org", "Jordan Pike",
        "Speaking slot at the maker meetup?",
        "We'd love to have you talk for 20 minutes in October. Travel is covered. "
        "Let me know if that's interesting and I'll send the details.",
        1.4, 0, 0, 1, "CONTENT", 0,
    ),
    (
        "main", "newsletter@example.org", "Toolshed Weekly",
        "Toolshed Weekly #182 — five jigs worth building",
        "This week: five jigs worth building, a reader gallery, and a deal on clamps. "
        "Unsubscribe any time using the link in the footer.",
        1.6, 1, 0, 9, "POTENTIAL_SPAM", 0,
    ),
    (
        "main", "billing@examp1e-secure.example.net", "Account Services",
        "Action required: verify your account within 24 hours",
        "Your account has been suspended due to unusual activity. Sign in within 24 hours "
        "to verify your identity or your mailbox will be deleted. Click here to log in now.",
        2.1, 0, 0, 3, "POTENTIAL_ISSUE", 1,
    ),
    (
        "main", "growth@example.net", "Digital Growth Partners",
        "Gave your website a fresh look — SEO proposal",
        "Dear website owner, we can get you on the 1st page of Google with backlinks and "
        "guest posts. Reply for a free audit and our digital marketing packages.",
        2.4, 0, 0, 4, "POTENTIAL_SPAM", 0,
    ),
    (
        "main", "casey.lin@example.com", "Casey Lin",
        "Permission to reprint a diagram",
        "I teach a community college course and would like to reprint one of your diagrams "
        "in a handout, with credit. Is that OK?",
        3.0, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "main", "robin.hale@example.com", "Robin Hale",
        "Broken download link on the templates page",
        "The ZIP on your templates page returns a 404 for me — tried two browsers.",
        3.3, 1, 0, 1, "CONTENT", 0,
    ),
    (
        "main", "press@example.org", "Sidebar Magazine",
        "Interview request for the autumn issue",
        "We're writing a feature on small studios and would like to include you. "
        "Five questions by email, published in October.",
        4.1, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "main", "no-reply@example.org", "Community Forum",
        "Your weekly digest: 12 new replies",
        "Here is what happened in the topics you follow this week.",
        4.5, 1, 0, 12, "POTENTIAL_SPAM", 0,
    ),
    (
        "main", "dana.olsen@example.com", "Dana Olsen",
        "Thanks for the quick answer",
        "That fixed it — appreciate the fast reply. Nothing else needed.",
        5.2, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "main", "hiring@example.net", "Talent Reach",
        "Developers available at short notice",
        "We have designers/developers available for app proposals and website redesigns. "
        "Rates attached.",
        5.8, 1, 1, 2, "POTENTIAL_SPAM", 0,
    ),
    (
        "main", "morgan.reed@example.com", "Morgan Reed",
        "Follow-up: shipping the sample back",
        "Posting the sample back today, tracking to follow. Thanks again for the loan.",
        6.4, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "shop", "quotes@supplier.example.com", "Northgate Supply",
        "Quotation Q-4471 for your material request",
        "Attached is quotation Q-4471 covering the sheet stock you asked about, "
        "valid for 30 days. Lead time is 6 working days from order.",
        0.5, 0, 1, 1, "CONTENT", 0,
    ),
    (
        "shop", "pat.novak@example.com", "Pat Novak",
        "My order status — order number 10488",
        "Hi, could you tell me where order 10488 is? It was due last Friday.",
        0.8, 0, 0, 0, "CONTENT", 0,
    ),
    (
        "shop", "lee.chan@example.com", "Lee Chan",
        "Product question: does it support 12 mm stock?",
        "Before I order — does the jig support 12 mm stock, or is 10 mm the maximum?",
        1.1, 0, 0, 0, "CONTENT", 0,
    ),
    (
        "shop", "ana.mercer@example.net", "Ana Mercer",
        "Quote for 40 units",
        "We'd like a quote for 40 units delivered to one address, plus availability "
        "and lead time.",
        1.9, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "shop", "returns@example.com", "Kim Farrow",
        "Damaged in transit — replacement request",
        "One of the two units arrived cracked. Photos attached. Happy to return it.",
        2.6, 0, 1, 0, "CONTENT", 0,
    ),
    (
        "shop", "accounts@examp1e-invoices.example.net", "Invoice Desk",
        "Invoice overdue — payment failed",
        "Your payment method was declined and your invoice is overdue. Update your payment "
        "details immediately to avoid deactivation.",
        3.4, 0, 0, 2, "POTENTIAL_ISSUE", 0,
    ),
    (
        "shop", "sales@example.net", "Bulk Lists Ltd",
        "Verified email lists, MX tested",
        "We sell verified email lists, MX tested, updated monthly. Ask for a sample file.",
        3.9, 1, 0, 5, "SPAM", 0,
    ),
    (
        "shop", "chris.dupont@example.org", "Chris Dupont",
        "Wholesale enquiry from a retailer",
        "We run three shops and would like to stock your kits. What are your wholesale "
        "terms and minimum order?",
        4.7, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "shop", "taylor.brooks@example.com", "Taylor Brooks",
        "Spec sheet for the mounting plate?",
        "Do you have a datasheet or specifications for the mounting plate, especially "
        "the hole pattern?",
        5.5, 1, 1, 0, "CONTENT", 0,
    ),
    (
        "shop", "noreply@example.org", "Courier Updates",
        "Delivery scheduled for tomorrow",
        "Your shipment is scheduled for delivery tomorrow between 09:00 and 13:00.",
        6.1, 1, 0, 3, "POTENTIAL_SPAM", 0,
    ),
    (
        "shop", "jamie.wolf@example.com", "Jamie Wolf",
        "Availability of the walnut variant",
        "Is the walnut variant back in stock, and what is the current lead time?",
        7.3, 1, 0, 0, "CONTENT", 0,
    ),
    (
        "shop", "harper.quinn@example.com", "Harper Quinn",
        "Thanks — order received",
        "Order arrived today, everything correct. Thanks for sorting the address change.",
        9.4, 1, 0, 0, "CONTENT", 0,
    ),
)


_DEMO_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 96>>stream\nBT /F1 14 Tf 20 100 Td (Quotation Q-4471 - demo document) Tj"
    b" 0 -24 Td (Fictional supplier, fictional prices.) Tj ET\nendstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _seed_attachment(store: Any, message_pk: int) -> None:
    """Store a tiny fictional PDF for a seeded message that claims an attachment,
    the same way the listener does (0600 file under data_dir()/attachments)."""
    import hashlib
    import os
    import stat

    from .paths import data_dir

    root = data_dir() / "attachments" / str(int(message_pk))
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, stat.S_IRWXU)
    target = root / "01_quotation-Q-4471.pdf"
    target.write_bytes(_DEMO_PDF)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    store.add_attachment(
        message_pk, "quotation-Q-4471.pdf", "application/pdf", len(_DEMO_PDF),
        str(target), sha256=hashlib.sha256(_DEMO_PDF).hexdigest(),
    )


def seed_demo(store: Any) -> dict[str, int]:
    """Populate ``store`` with the fictional demo dataset.

    Idempotent: seeding an already-seeded store inserts nothing new (message
    identity is ``(account, folder, uid)``). Returns simple counts.
    """
    from .db.store import register_sites

    register_sites(DEMO_SITES.keys())

    accounts = {
        "main": store.upsert_account(
            "main", "imap", "imap.example.com", 993, "hello@example.com", "main"
        ),
        "shop": store.upsert_account(
            "shop", "imap", "imap.example.com", 993, "orders@example.net", "shop"
        ),
    }

    inserted = 0
    threads: dict[str, str] = {}
    message_ids: dict[str, int] = {}
    for uid, row in enumerate(_MESSAGES, start=101):
        (
            site, from_addr, from_name, subject, body, days_ago, seen,
            has_attachments, links, screening, quarantined,
        ) = row
        base = subject.removeprefix("Re: ")
        thread_id = threads.setdefault(base, f"demo-thread-{len(threads) + 1}")
        mid = store.insert_message(
            account_id=accounts[site],
            folder="INBOX",
            uid=uid,
            message_id=f"<demo-{uid}@example.com>",
            thread_id=thread_id,
            from_addr=from_addr,
            from_name=from_name,
            to_addrs="hello@example.com" if site == "main" else "orders@example.net",
            cc_addrs="",
            subject=subject,
            received_at=_iso(days_ago),
            raw_html=None,
            sanitized_text=body,
            has_attachments=int(bool(has_attachments)),
            link_count=int(links),
            quarantined=int(quarantined),
            quarantine_reason=("ingest injection score 0.94" if quarantined else None),
            screening_status=screening,
            screening_reason=(
                "matches the site's expected content topics"
                if screening == "CONTENT"
                else "not clearly related to the site's published content"
            ),
        )
        if mid is None:
            continue
        inserted += 1
        if seen:
            store.mark_message_seen(mid)
        message_ids[subject] = mid
        if has_attachments:
            _seed_attachment(store, mid)

    drafts = 0
    pending = message_ids.get("Question about your workshop guide")
    if pending is not None:
        store.create_draft(
            message_id=pending,
            thread_id="demo-thread-1",
            recipient="alex.rivera@example.com",
            subject="Re: Question about your workshop guide",
            body=(
                "Hi Alex,\n\nThanks for reading. Yes — for thicker stock move the clamp "
                "one hole back so the jig still sits flat.\n\nBest,\nAlex Example"
            ),
            state="PENDING",
            sender_addr="hello@example.com",
            site_id="main",
        )
        drafts += 1

    replied = message_ids.get("Permission to reprint a diagram")
    if replied is not None:
        draft_id = store.create_draft(
            message_id=replied,
            thread_id=threads.get("Permission to reprint a diagram", "demo-thread-7"),
            recipient="casey.lin@example.com",
            subject="Re: Permission to reprint a diagram",
            body=(
                "Hi Casey,\n\nYes, please go ahead with credit and a link back.\n\n"
                "Best,\nAlex Example"
            ),
            state="APPROVED",
            sender_addr="hello@example.com",
            site_id="main",
        )
        store.update_draft_state(draft_id, "SENT", sent_at=_iso(2.8))
        drafts += 1

    return {"accounts": len(accounts), "messages": inserted, "drafts": drafts}


def build_demo_home(root: Path) -> Path:
    """Create ``<root>/config/config.toml`` and return the application home."""
    root = Path(root)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.toml").write_text(DEMO_CONFIG_TOML, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Simulated mailbox: lets the header sync chip and the Refresh button behave
# as they do with a real account. Nothing is fetched — it only reports status.
# --------------------------------------------------------------------------- #
class SimulatedMailbox:
    """Registers as a mailbox in the live sync registry and "checks" on a timer.

    The demo has no IMAP account, so without this the header would read
    "No mailboxes". Each check finds nothing new (the seeded store is
    static); a Refresh click completes within about a second like the real
    listener. Clearly named so nobody mistakes it for a real connection.
    """

    def __init__(self, name: str = "demo-mailbox (simulated)", period_s: float = 30.0) -> None:
        import threading

        self.name = name
        self.period_s = float(period_s)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import threading
        import time

        from .mail.sync_state import registry

        registry.register(self.name, self.request_refresh)
        registry.update(self.name, state="idle", connected_since=time.time())

        def _run() -> None:
            registry.note_check(self.name, 0)
            while not self._stop.is_set():
                self._wake.wait(self.period_s)
                self._wake.clear()
                if self._stop.is_set():
                    break
                registry.update(self.name, state="checking")
                time.sleep(0.4)  # visible "Checking mail…" blink
                registry.note_check(self.name, 0)

        self._thread = threading.Thread(target=_run, name="demo-mailbox", daemon=True)
        self._thread.start()

    def request_refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
