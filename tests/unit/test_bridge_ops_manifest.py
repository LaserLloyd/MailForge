"""The bridge's action list is frozen and classified; the agent boundary is enforced by AST.

Adding an op without classifying it here fails. The bridge has NO send action and
must not be able to reach SMTP or any human-only mutation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mailforge import bridge_cli
from mailforge.bridge_cli import OP_CLASS, OPS

# class: read | draft_write | triage | guidance_write
FROZEN_BASE = {
    "status": "read", "list": "read", "get": "read", "weekly-summary": "read",
    "daily-brief": "read", "reference-search": "read", "notes": "read",
    "drafts": "read", "draft": "read",
    "propose": "draft_write", "revise": "draft_write", "compose": "draft_write",
    "reply": "draft_write", "edit-draft": "draft_write", "withdraw-draft": "draft_write",
    "note-add": "triage",
    "attachments": "read", "outbox": "read", "sent": "read", "trash-queue": "read",
    "config-summary": "read", "activity": "read",
    "mark-read": "triage", "archive": "triage", "trash": "triage", "flag": "triage",
}
# Owned by the guidance work package; only classes are pinned here.
FROZEN_GUIDANCE = {
    "templates": "read", "template": "read", "template-save": "guidance_write",
    "template-delete": "guidance_write", "references": "read", "reference": "read",
    "reference-save": "guidance_write", "reference-delete": "guidance_write",
    "reference-reindex": "guidance_write", "guidance": "read", "policy-save": "guidance_write",
    "guidance-refresh": "guidance_write", "guidance-history": "read",
    "guidance-revert": "guidance_write",
}
SRC = Path(bridge_cli.__file__).parent


def test_ops_are_exactly_the_classified_ops():
    assert set(FROZEN_BASE) <= set(OPS)
    extra = set(OPS) - set(FROZEN_BASE)
    assert extra <= set(FROZEN_GUIDANCE), f"unclassified ops: {sorted(extra - set(FROZEN_GUIDANCE))}"
    assert set(OP_CLASS) == set(OPS)
    for op, cls in OP_CLASS.items():
        assert cls == {**FROZEN_BASE, **FROZEN_GUIDANCE}[op], op
    assert set(OP_CLASS.values()) <= {"read", "draft_write", "triage", "guidance_write"}


def test_every_op_has_a_handler():
    assert set(bridge_cli._registry()) == set(OPS)


def test_no_send_class_op_exists():
    for name in ("send", "prepare-send", "prepare_send", "send-draft", "approve"):
        assert name not in OPS
    assert not any("send" in op and op not in {"sent"} for op in OPS)
    assert "gated_send" not in OP_CLASS.values()


def test_agent_send_flag_is_still_readable_but_unused_by_the_bridge():
    from mailforge.config import Settings

    assert Settings(openclaw={"agent_send_enabled": True}).openclaw.agent_send_enabled is True
    for path in _bridge_files():
        assert "agent_send" not in path.read_text(encoding="utf-8"), path


def _bridge_files() -> list[Path]:
    files = [SRC / "bridge_cli.py"]
    files += sorted((SRC / "bridge_ops").glob("*.py"))
    return files


FORBIDDEN_NAMES = {
    "set_message_quarantined", "purge_messages", "record_allowlist", "transmit_and_record",
    "send_email", "get_smtp_secret", "smtp_for_account", "SMTP", "SMTP_SSL", "smtplib",
    "smtp_sender", "set_smtp_secret", "set_imap_secret",
}
FORBIDDEN_PREFIXES = ("purge_now", "set_", "save_settings", "save_added_account")
ALLOWED_SET_PREFIX = {"set_messages_archived", "set_messages_trashed"}


def _names(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id, node
        elif isinstance(node, ast.Attribute):
            yield node.attr, node
        elif isinstance(node, ast.alias):
            yield node.name.split(".")[-1], node
        elif isinstance(node, ast.ImportFrom) and node.module:
            for part in node.module.split("."):
                yield part, node
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            pass


@pytest.mark.parametrize("path", _bridge_files(), ids=lambda p: p.name)
def test_bridge_code_cannot_reach_send_or_human_only_mutations(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for name, node in _names(tree):
        if name in FORBIDDEN_NAMES or name in {"secrets"} and isinstance(node, ast.ImportFrom):
            bad.append((name, node.lineno))
        elif name.startswith(FORBIDDEN_PREFIXES) and name not in ALLOWED_SET_PREFIX:
            bad.append((name, node.lineno))
    assert not bad, f"{path.name} reaches forbidden code: {bad}"


@pytest.mark.parametrize("path", _bridge_files(), ids=lambda p: p.name)
def test_screening_feedback_only_flags_never_spam_or_content(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "record_screening_feedback":
            labels = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            labels += [k.value.value for k in node.keywords
                       if k.arg == "label" and isinstance(k.value, ast.Constant)]
            assert labels, "label must be a literal so it can be checked"
            assert all(lbl in {"POTENTIAL_SPAM", "POTENTIAL_ISSUE"} for lbl in labels), labels
            learn = [k.value for k in node.keywords if k.arg == "learn_similar"]
            assert learn and isinstance(learn[0], ast.Constant) and learn[0].value is False
