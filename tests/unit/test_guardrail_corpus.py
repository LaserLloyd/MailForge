"""Ordinary business strings through the secret and PII detectors.

``CLEAN`` rows must produce no verdict from the always-available heuristic
detectors (``secrets_scan.scan`` and ``presidio.detect_pii``). ``KNOWN`` rows
pin the verdict for strings that ARE detected, either correctly or as a
documented limitation of a format-only detector, so a behaviour change is
noticed.

Known limitations (format-only detection cannot tell these apart):
* A US-SSN-shaped part or case number (``555-12-3456``) is reported as US_SSN.
* Japanese phone numbers (``03-1234-5678``) are NOT reported as phone numbers:
  the heuristic phone pattern is North-American only.
* An email address is reported as EMAIL_ADDRESS (correct; whether it blocks is
  decided by ``pii_block_categories``).
"""

from __future__ import annotations

import pytest

from mailforge.security import presidio, secrets_scan

CLEAN: list[str] = [
    "Order #A-20261102-0042 shipped via Yamato, tracking 4412-8890-1234.",
    "Invoice INV-2026-001234 is due 2026-11-30.",
    "Tracking number 1234567890123 for your parcel.",
    "Call us on 03-1234-5678 weekdays 9:00-18:00.",
    "Mobile 090-1234-5678 for urgent delivery matters.",
    "International +81 3 1234 5678 reaches our Tokyo office.",
    "Our office postal code is 150-0002, Shibuya, Tokyo.",
    "Postal code 〒100-0001 Chiyoda.",
    "The price is ¥12,800 including tax (JPY 12800).",
    "Total: $1,299.00 USD plus shipping of $24.50.",
    "Order reference 20261102123456 was confirmed.",
    "Ticket TKT-8841-2201-9983 has been opened.",
    "Your SKU is ACME-LICENSE-STD-2026-PRO-ANNUAL-0001.",
    "Version 2.14.1234 build 20261102.1234 released.",
    "Quote 202611-0001234 valid until 2026-12-15.",
    "Reference number 4000 1234 5678 is not a card number.",
    "Meeting at 2026-11-04T09:30:00+09:00 in room 4B.",
    "See https://example.com/guides/getting-started-with-the-product for details.",
    "The file name is firmware_package_model_m7_v2_14_final.zip as requested.",
    "Case ID 2026110200042 and customer ID 00123456 are linked.",
    "Amount due 1,234,567 yen by 2026/11/30.",
    "Fax 03-5555-1212 is no longer used.",
    "Order 123-4567890-1234567 from the marketplace.",
    "Our invoice registration number is T1234567890123.",
    "Hello Tanaka-san, thank you for your inquiry about licensing.",
    "Please reply by 5pm JST; we are UTC+9.",
    "Shipment weight 12.5kg, dimensions 30x20x10 cm.",
    "Booking confirmation XK29-PL77-QR45 for 2 guests.",
    "The manifest lists a checksum starting 9f86d081 for the download.",
    "Session of 2026-11-02 14:00 to 15:30.",
    "Zip code 98101 Seattle, WA.",
    "Credit of 5,000 points expires 2027-03-31.",
    "Order number 2026-11-02-000123-AB shipped.",
    "Refund REF-20261102-7788 processed.",
    "Docs at /guides/quick-start/installation-and-first-run-checklist-v2.",
    "The API accepts a field named api_key_header in requests.",
    "We reset passwords through the account page, never by email.",
    "Account number ending 1234 on file.",
    "Tokyo Station to Shibuya takes 25 minutes, ¥170.",
    "Read more at https://example.com/blog/2026/11/how-to-license-your-music-for-film.",
    "Download release_notes_for_the_november_update_2026.pdf from the portal.",
    "Attached: scan_of_signed_licence_agreement_final_v3.pdf (240 KB).",
    "Delivery window 2026-11-05 between 14:00 and 16:00, signature required.",
]

# (text, expected secret types, expected PII types)
KNOWN: list[tuple[str, list[str], list[str]]] = [
    ("Contact support@example.com for help.", [], ["EMAIL_ADDRESS"]),
    ("Part number 555-12-3456 is back in stock.", [], ["US_SSN"]),
    ("The invoice total is 123-45-6789 units.", [], ["US_SSN"]),
    # real-looking card numbers (Luhn valid, real issuer prefixes) are still caught
    ("Card 4111 1111 1111 1111 expires 12/28.", [], ["CREDIT_CARD"]),
    ("Card 5500-0000-0000-0004 on file.", [], ["CREDIT_CARD"]),
    ("Card 378282246310005 on file.", [], ["CREDIT_CARD"]),
    ("Call (415) 555-0132 today.", [], ["PHONE_NUMBER"]),
    # real credentials are still caught
    ("password: hunter2hunter2", ["KeywordDetector"], []),  # scrub-check: allow
    ("key AKIAIOSFODNN7EXAMPLE", ["AWSKeyDetector"], []),  # scrub-check: allow
    (
        "token ghp_aB3dE5gHjKmNpQsTvWyZbCdEfGhJkLmNpQrS",  # scrub-check: allow
        ["GitHubTokenDetector", "HighEntropyString"],
        [],
    ),
    (
        "Authorization: Bearer abcdEFGH1234ijklMNOP5678",  # scrub-check: allow
        ["BearerTokenDetector", "HighEntropyString"],
        [],
    ),
    (
        "secret Xk9fQ2vLm7ZpR4tYb8WnC3jHs6DgA1eU",  # scrub-check: allow
        ["HighEntropyString"],
        [],
    ),
]


def test_corpus_has_at_least_forty_clean_strings():
    assert len(CLEAN) >= 40


@pytest.mark.parametrize("text", CLEAN)
def test_ordinary_business_text_is_not_flagged(text: str):
    assert secrets_scan.scan(text) == [], text
    assert presidio.detect_pii(text) == [], text


@pytest.mark.parametrize("text,secrets,pii", KNOWN)
def test_known_verdicts_are_pinned(text: str, secrets: list[str], pii: list[str]):
    assert secrets_scan.scan(text) == secrets
    assert presidio.detect_pii(text) == pii
