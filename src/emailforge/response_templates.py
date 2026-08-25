"""Site-scoped response templates and safe placeholder expansion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

ALLOWED_PLACEHOLDERS = frozenset(
    {"first_name", "site_name", "signature", "today", "question", "next_step"}
)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


@dataclass(frozen=True)
class ExampleTemplate:
    """A site-agnostic starter template (no site binding)."""

    name: str
    category: str
    subject: str
    body: str
    shortcut: str = ""


@dataclass(frozen=True)
class DefaultTemplate:
    """An example template bound to a concrete site id, ready to seed."""

    site_id: str
    name: str
    category: str
    subject: str
    body: str
    shortcut: str = ""


def validate_template_text(text: str) -> None:
    unknown = sorted(set(_PLACEHOLDER_RE.findall(str(text or ""))) - ALLOWED_PLACEHOLDERS)
    if unknown:
        raise ValueError(f"unsupported template placeholder(s): {', '.join(unknown)}")


def expand_template(text: str, **values: Any) -> str:
    replacements = {
        "site_name": str(values.get("site_name") or ""),
        "signature": str(values.get("signature") or ""),
        "today": str(values.get("today") or date.today().isoformat()),
        "first_name": str(values.get("first_name") or "{{first_name}}"),
        "question": str(values.get("question") or "{{question}}"),
        "next_step": str(values.get("next_step") or "{{next_step}}"),
    }

    def replace(match: re.Match[str]) -> str:
        return replacements.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(replace, str(text or ""))


#: Site-agnostic example templates seeded into a brand-new database (one copy
#: per configured site, see ``Store._seed_response_templates``). They are plain
#: examples: edit or delete them in the Templates page. Every business-specific
#: sentence belongs in your own templates, not here — placeholders keep these
#: neutral: ``{{site_name}}`` is the site's configured display name and
#: ``{{signature}}`` comes from ``[compose] signature`` in config.toml.
EXAMPLE_TEMPLATES: tuple[ExampleTemplate, ...] = (
    ExampleTemplate(
        "General acknowledgment",
        "General",
        "Re: {{question}}",
        """Hi {{first_name}},

Thanks for reaching out to {{site_name}}. I have your message and will look at the details.

{{next_step}}

Best,
{{signature}}""",
        "ack",
    ),
    ExampleTemplate(
        "Request more information",
        "Support",
        "Re: {{question}}",
        """Hi {{first_name}},

Thanks for contacting {{site_name}}. So I can answer accurately, could you send:

- What you were trying to do, and what happened instead
- Any reference, order, or document number involved
- Relevant dates and the exact error text, if there is one

Please do not send passwords, keys, or payment credentials by email.

Best,
{{signature}}""",
        "more-info",
    ),
    ExampleTemplate(
        "Answer with next step",
        "Support",
        "Re: {{question}}",
        """Hi {{first_name}},

Short answer: {{question}}

{{next_step}}

If that does not match what you are seeing, reply here and I will take another look.

Best,
{{signature}}""",
        "next-step",
    ),
    ExampleTemplate(
        "Not the right contact",
        "Routing",
        "Re: {{question}}",
        """Hi {{first_name}},

Thanks for writing. This inbox is not the right place for that request, so it would only sit here.

{{next_step}}

Best,
{{signature}}""",
        "route",
    ),
    ExampleTemplate(
        "Close the thread",
        "General",
        "Re: {{question}}",
        """Hi {{first_name}},

Glad that worked. I will close this out on my side — just reply here if anything else comes up.

Best,
{{signature}}""",
        "close",
    ),
)


def default_templates_for(site_id: str) -> tuple[DefaultTemplate, ...]:
    """The example template set bound to one site id."""
    site = str(site_id or "").strip().lower()
    return tuple(
        DefaultTemplate(site, t.name, t.category, t.subject, t.body, t.shortcut)
        for t in EXAMPLE_TEMPLATES
    )
