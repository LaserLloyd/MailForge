"""Turn a pytest JUnit XML report into GitHub Actions error annotations.

CI job logs are not always readable (a fork, a logged-out browser, an API
client without a token), but annotations are shown on the run page and served
by the public check-runs API. So the test job writes ``--junitxml`` and this
script prints one ``::error`` line per failed or errored test, plus a summary
line naming every failure, so a red run says what broke without the log.

Standard library only, so it runs before or without the project's own
dependencies. It never fails the job itself: the test step's exit code does.

Usage: python scripts/junit_annotations.py pytest-results.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# GitHub shows at most 10 annotations of each level per step; keep one error
# slot for the summary line.
_DETAILED = 9
_TAIL_LINES = 12


def _escape(text: str) -> str:
    """Escape annotation data per the workflow-command rules."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_prop(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def _location(case: ET.Element) -> tuple[str, str]:
    file = case.get("file") or ""
    line = case.get("line") or ""
    if not file:
        classname = case.get("classname") or ""
        parts = classname.split(".")
        # tests.unit.test_x.TestClass -> tests/unit/test_x.py
        while parts and not parts[-1].startswith("test"):
            parts.pop()
        if parts:
            file = "/".join(parts) + ".py"
    if line.isdigit():
        line = str(int(line) + 1)  # JUnit lines are 0-based
    return file.replace("\\", "/"), line


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: junit_annotations.py REPORT.xml", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.is_file():
        print(f"::error::pytest wrote no JUnit report at {report} "
              "(crashed before collection?)")
        return 0
    try:
        root = ET.parse(report).getroot()
    except ET.ParseError as e:
        print(f"::error::unreadable JUnit report {report}: {_escape(str(e))}")
        return 0

    failures: list[str] = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        classname = case.get("classname") or ""
        test_id = f"{classname}::{case.get('name') or '?'}".lstrip(":")
        failures.append(test_id)
        if len(failures) > _DETAILED:
            continue
        message = problem.get("message") or ""
        body = (problem.text or "").strip().splitlines()
        tail = "\n".join(body[-_TAIL_LINES:])
        text = f"{test_id} [{problem.tag}]: {message}\n{tail}".strip()
        file, line = _location(case)
        props = []
        if file:
            props.append(f"file={_escape_prop(file)}")
            if line:
                props.append(f"line={line}")
        props.append(f"title={_escape_prop(test_id)[:200]}")
        print(f"::error {','.join(props)}::{_escape(text[:4000])}")

    if failures:
        listing = "\n".join(failures[:60])
        more = f"\n... and {len(failures) - 60} more" if len(failures) > 60 else ""
        print(f"::error title={len(failures)} failing tests::"
              f"{_escape(listing + more)}")
    else:
        print("JUnit report: no failures or errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
