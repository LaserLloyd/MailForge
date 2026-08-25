"""CI has to actually gate the things it claims to gate.

A workflow is configuration, not code, so nothing else in this suite notices
when a job is missing or an action major goes end-of-life — the failure mode is
a green tick on a run that checked less than the README says it does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def test_a_blocking_privacy_job_exists(ci):
    """The three sibling repositories all gate publication in CI. This one had
    no privacy job at all: lint and tests only, so nothing on the server side
    ever asked whether private data was in the tree."""
    assert "privacy" in ci["jobs"], sorted(ci["jobs"])
    job = ci["jobs"]["privacy"]
    # No `continue-on-error`: a privacy warning that does not fail the run is a
    # leak with a green tick on it.
    assert job.get("continue-on-error") in (None, False)
    for step in _steps(job):
        assert step.get("continue-on-error") in (None, False), step


def test_the_privacy_job_scans_tree_selftest_and_commits(ci):
    run = "\n".join(s.get("run", "") for s in _steps(ci["jobs"]["privacy"]))
    assert "--self-test" in run, "the scanner's own rules must be proved"
    assert "scrub_check.py" in run
    assert "--commit-range" in run, "commit messages publish too"
    assert "--rev" in run and "rev-list" in run, (
        "every commit's TREE must be scanned, not just the head — a secret "
        "added in one commit and removed in the next still publishes")


def test_the_privacy_job_fetches_full_history(ci):
    """A shallow clone does not contain the PR base commit, and the range scan
    then dies with 'unknown revision' — which reads as a broken job, not a
    leak, and gets muted."""
    checkouts = [s for s in _steps(ci["jobs"]["privacy"])
                 if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts, "the privacy job must check out the repository"
    assert all(s.get("with", {}).get("fetch-depth") == 0 for s in checkouts), checkouts


#: Majors still receiving updates. v4/v5 are the end-of-life pair this repo
#: shipped with; the sibling repositories already run these.
MINIMUM_MAJOR = {"actions/checkout": 6, "astral-sh/setup-uv": 10}


def test_no_deprecated_action_majors(ci):
    stale = []
    for name, job in ci["jobs"].items():
        for step in _steps(job):
            uses = str(step.get("uses", ""))
            if "@v" not in uses:
                continue
            action, _, ref = uses.partition("@")
            floor = MINIMUM_MAJOR.get(action)
            if floor is None:
                continue
            major = int(ref.lstrip("v").split(".")[0])
            if major < floor:
                stale.append(f"{name}: {uses} (needs >= v{floor})")
    assert stale == [], stale
