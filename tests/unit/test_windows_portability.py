"""Static guards for the Windows leg of CI, provable on Linux.

CI runs the suite on ``windows-latest`` as well as Linux, and nobody develops
on Windows — so the patterns that only fail there are pinned down here by
reading the source rather than by hoping someone notices a red run.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/mailforge", "tests", "scripts")

#: Text-mode readers/writers whose default encoding is the locale's — cp1252
#: on a Windows runner, so any non-ASCII byte raises UnicodeDecodeError.
_TEXT_IO = {"read_text", "write_text", "fdopen"}


def _sources() -> list[Path]:
    files: list[Path] = []
    for where in SCANNED:
        files.extend(sorted((ROOT / where).rglob("*.py")))
    assert files, "no sources found to scan"
    return files


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {k.arg: k.value for k in call.keywords if k.arg}


def _literal_mode(call: ast.Call, positional: int) -> str | None:
    node = _kwargs(call).get("mode")
    if node is None and len(call.args) > positional:
        node = call.args[positional]
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


@pytest.fixture(scope="module")
def parsed() -> list[tuple[Path, ast.AST]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"), str(p))) for p in _sources()]


def test_named_temporary_files_are_closed_before_reuse(parsed):
    """Windows refuses a second open of a live NamedTemporaryFile.

    ``delete=False`` (plus an explicit unlink) is the portable shape whenever
    the name is handed to anything else.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{call.lineno}"
        for path, tree in parsed
        for call in _calls(tree)
        if _name(call) == "NamedTemporaryFile"
        and not (
            isinstance(_kwargs(call).get("delete"), ast.Constant)
            and _kwargs(call)["delete"].value is False
        )
    ]
    assert offenders == [], f"NamedTemporaryFile without delete=False: {offenders}"


def test_text_io_declares_an_encoding(parsed):
    """No text read/write may inherit the locale encoding."""
    offenders = []
    for path, tree in parsed:
        for call in _calls(tree):
            name = _name(call)
            kwargs = _kwargs(call)
            if name in _TEXT_IO:
                positional_encoding = name in {"read_text", "write_text"} and len(call.args) > (
                    1 if name == "write_text" else 0
                )
                if name == "fdopen" and (_literal_mode(call, 1) or "r").find("b") >= 0:
                    continue
                if "encoding" in kwargs or positional_encoding:
                    continue
            elif isinstance(call.func, ast.Name) and name == "open":
                if "b" in (_literal_mode(call, 1) or "r") or "encoding" in kwargs:
                    continue
            else:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{call.lineno}: {name}()")
    assert offenders == [], f"text I/O without encoding=: {offenders}"


def test_no_os_rename_or_shell_true(parsed):
    """``os.rename`` fails over an existing destination on Windows.

    ``os.replace`` is the atomic, portable one. ``shell=True`` drags in
    ``cmd.exe`` quoting rules, so it is banned outright.
    """
    offenders = []
    for path, tree in parsed:
        for call in _calls(tree):
            if _name(call) == "rename" and isinstance(call.func, ast.Attribute):
                offenders.append(f"{path.relative_to(ROOT)}:{call.lineno}: os.rename")
            shell = _kwargs(call).get("shell")
            if isinstance(shell, ast.Constant) and shell.value is True:
                offenders.append(f"{path.relative_to(ROOT)}:{call.lineno}: shell=True")
    assert offenders == [], offenders


def test_no_posix_only_modules_are_imported(parsed):
    """These have no Windows implementation: importing one breaks collection."""
    banned = {"fcntl", "pwd", "grp", "termios", "resource", "posix"}
    offenders = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in banned:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    assert offenders == [], offenders


def test_private_mode_helper_is_a_noop_on_windows(tmp_path, monkeypatch, assert_private_mode):
    """The 0600 assertions must not fail a Windows runner (no mode bits there)."""
    loose = tmp_path / "loose.txt"
    loose.write_text("x", encoding="utf-8")
    loose.chmod(0o644)

    if sys.platform != "win32":
        with pytest.raises(AssertionError):
            assert_private_mode(loose)  # strict where modes exist

    monkeypatch.setattr(sys, "platform", "win32")
    assert_private_mode(loose)  # tolerated where the OS has no such bits

    with pytest.raises(AssertionError):
        assert_private_mode(tmp_path / "missing.txt")  # still catches "never written"


def test_detect_secrets_bridge_writes_utf8_and_closes_before_scanning(tmp_path, monkeypatch):
    """The detect-secrets hand-off must survive Windows file locking.

    ``scan_file()`` reopens the temp file BY NAME, which Windows refuses while
    the ``NamedTemporaryFile`` handle is open — and the email text it carries
    is not ASCII, so the write has to name UTF-8 rather than inherit cp1252.
    Both are asserted on the call itself, so this holds on any platform.
    """
    import tempfile
    import types

    from mailforge.security import secrets_scan

    seen: dict[str, object] = {}
    real = tempfile.NamedTemporaryFile

    def recording(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", recording)

    scanned: dict[str, str] = {}

    class FakeCollection:
        def scan_file(self, name: str) -> None:
            # Reopen by name, exactly as detect-secrets does.
            scanned["text"] = Path(name).read_text(encoding="utf-8")

        def __iter__(self):
            return iter(())

    fake = types.ModuleType("detect_secrets")
    fake.SecretsCollection = FakeCollection  # type: ignore[attr-defined]
    settings = types.ModuleType("detect_secrets.settings")

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    settings.default_settings = _Ctx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "detect_secrets", fake)
    monkeypatch.setitem(sys.modules, "detect_secrets.settings", settings)

    secrets_scan.scan("naïve — señor writes AKIAIOSFODNN7EXAMPLE")  # scrub-check: allow

    assert seen.get("encoding") == "utf-8", "temp write must not inherit cp1252"
    assert seen.get("delete") is False, "file must be closed (not auto-deleted) before reopening"
    assert "naïve — señor" in scanned["text"]


def test_atomic_private_write_survives_a_missing_os_fchmod(tmp_path, monkeypatch):
    """``os.fchmod`` does not exist on Windows.

    It is the single write path for every site handbook, so an unguarded call
    turns the whole feature into an ``AttributeError`` on the Windows leg of
    CI. Deleting the attribute reproduces that platform on Linux.
    """
    import os as os_module

    from mailforge.knowledge import handbooks

    monkeypatch.delattr(os_module, "fchmod", raising=True)

    target = tmp_path / "handbook.md"
    written = handbooks._atomic_private_write(target, "body\n")

    assert written.read_text(encoding="utf-8") == "body\n"
