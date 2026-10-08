#!/usr/bin/env python3
"""Run every agent stage against real Claude Code on a tiny repository.

This is the check that a Claude Code version works with NexKit: it exercises the real
CLI flags, stream output and structured results that unit tests replace with a fake.
It costs a few cents (model `haiku`). Needs a Claude login or credential.

    scripts/smoke.py [--claude PATH]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nexkit  # noqa: E402
from nexkit.agent import run_stage, run_triage  # noqa: E402
from nexkit.config import validate  # noqa: E402
from nexkit.context import previous_review  # noqa: E402
from nexkit.publish import pull_body, render_plan  # noqa: E402
from nexkit.report import review_body  # noqa: E402
from nexkit.state import BOT_LOGIN, PLAN_MARKER, empty_state, render_state  # noqa: E402

CONFIG = validate({"model": "haiku", "checks": [{"name": "test", "run": "python3 -m unittest -v"}]})
CONTEXT = {
    "issue": 1,
    "pr": "",
    "title": "add() returns the wrong result",
    "body": "calc.add(2, 3) returns -1. It should return 5.",
    "discussion": "No discussion.",
    "outside_plan": "None.",
    "plan": "No plan was posted. Implement the issue as described.",
    "note": "None.",
    "feedback": "",
}


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root):
    origin = root / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (origin / "test_calc.py").write_text(
        "import unittest\n\nfrom calc import add\n\n\n"
        "class CalcTests(unittest.TestCase):\n"
        "    def test_add_zero(self):\n"
        "        self.assertEqual(add(0, 0), 0)\n"
    )
    git(origin, "add", "-A")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    work = root / "work"
    git(root, "clone", "-q", str(origin), str(work))
    return work


class StateComments:
    """Just enough of the GitHub client for `previous_review`."""

    def __init__(self, state):
        self.body = render_state(state)

    def comments(self, number):
        return [{"id": 1, "body": self.body, "user": {"login": BOT_LOGIN}}]


def stage(name, work, out, claude, context=None, *, want="done", **kw):
    result = run_stage(name, context or CONTEXT, CONFIG, work, out, claude=claude, **kw)
    keys = ("status", "error", "cost", "changed_files", "conflicts")
    summary = {k: result.get(k) for k in keys}
    print(f"{name}: {json.dumps(summary)}", flush=True)
    if result["status"] != want:
        raise SystemExit(f"{name} stage failed: {result.get('error')}")
    # The readable transcript pairs every tool call with its result.
    transcript = (Path(out) / "transcript.md").read_text()
    if "\n### [" not in transcript or "Result: no result" in transcript:
        raise SystemExit(f"{name}: transcript.md lacks tool calls with results")
    return result


def triage(root, claude):
    """Triage picks a profile from the issue text, and a collaborator's request wins."""
    cfg = validate(
        {
            "model": "haiku",
            "default_profile": "standard",
            "profiles": {
                "standard": {"when": "A small, clear change to one function."},
                "hard": {"when": "Changes authentication, permissions or the storage format."},
            },
        }
    )
    cases = [
        ({**CONTEXT, "requests": "No discussion."}, "standard"),
        ({**CONTEXT, "requests": "@olivia: Plan this with the hard profile."}, "hard"),
    ]
    for number, (context, want) in enumerate(cases, 1):
        result = run_triage(context, cfg, None, root / f"triage-{number}", claude=claude)
        keys = ("status", "profile", "chosen_by", "reason", "error", "cost")
        print(f"triage: {json.dumps({k: result.get(k) for k in keys})}", flush=True)
        if result["chosen_by"] != "triage" or result["profile"] != want or not result["reason"]:
            raise SystemExit(f"triage: wanted {want}, got {result}")


def conflicted(root, name, path, start, base, pull):
    """A checkout of branch nexkit/issue-1 whose change to `path` conflicts with main's.
    `base` and `pull` are (content, commit message)."""
    origin = root / name
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam"]
    (origin / path).write_text(start)
    git(origin, "add", "-A")
    git(origin, *commit, "init")
    git(origin, "checkout", "-q", "-b", "nexkit/issue-1")
    (origin / path).write_text(pull[0])
    git(origin, *commit, pull[1])
    git(origin, "checkout", "-q", "main")
    (origin / path).write_text(base[0])
    git(origin, *commit, base[1])
    work = root / f"{name}-work"
    git(root, "clone", "-q", "-b", "nexkit/issue-1", str(origin), str(work))
    return work


def conflicts(root, claude):
    """Fix rounds on pull requests that conflict with main: one conflict has one right
    answer, the other needs a person's decision, which a note then gives."""
    context = {
        **CONTEXT,
        "pr": 2,
        "title": "Add small helper functions",
        "body": "Add the helper functions this project needs.",
        "plan": "No plan was posted. Implement the issue as described.",
        "feedback": "No recorded feedback. Follow the note.",
    }
    calc = "def add(a, b):\n    return a + b\n"
    work = conflicted(
        root,
        "trivial",
        "calc.py",
        calc,
        (calc + "\n\ndef mul(a, b):\n    return a * b\n", "Add mul()"),
        (calc + "\n\ndef sub(a, b):\n    return a - b\n", "Add sub()"),
    )
    fix = stage("fix", work, root / "fix-trivial", claude, context, base="main")
    merged = (work / "calc.py").read_text()
    if fix["conflicts"] != ["calc.py"] or "def mul" not in merged or "def sub" not in merged:
        raise SystemExit(f"fix: both sides were not kept:\n{merged}")

    greet = "def greeting(name):\n    return {}\n"
    choice = (
        root,
        "choice",
        "greet.py",
        greet.format('"Hello " + name'),
        (greet.format('"Welcome, " + name + "!"'), "Greet visitors with Welcome"),
        (greet.format('"Hi " + name'), "Greet visitors with Hi"),
    )
    work = conflicted(*choice)
    blocked = stage("fix", work, root / "fix-choice", claude, context, base="main", want="blocked")
    questions = blocked["output"]["open_conflicts"]
    print(f"open conflicts: {json.dumps(questions)}", flush=True)
    if not any(item["file"] == "greet.py" and item["question"] for item in questions):
        raise SystemExit("fix: the open conflict in greet.py was not listed")

    shutil.rmtree(root / "choice")
    shutil.rmtree(root / "choice-work")
    work = conflicted(*choice)
    note = 'In greet.py, keep this pull request\'s greeting: "Hi " + name.'
    stage("fix", work, root / "fix-note", claude, {**context, "note": note}, base="main")
    if (work / "greet.py").read_text() != greet.format('"Hi " + name'):
        raise SystemExit("fix: the note's decision was not followed")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude", default="claude")
    args = parser.parse_args()

    version = subprocess.run(
        [args.claude, "--version"], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    print(f"Claude Code {version}; NexKit pins {nexkit.CLAUDE_CODE}", flush=True)
    if version != nexkit.CLAUDE_CODE:
        raise SystemExit("The installed Claude Code is not the pinned version.")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work = make_repo(root)

        triage(root, args.claude)
        plan = stage("plan", work, root / "plan", args.claude)
        for field in ("changes", "acceptance_criteria", "implementation_notes"):
            if not plan["output"][field]:
                raise SystemExit(f"plan: no {field}")
        CONTEXT["plan"] = render_plan(plan["output"]).removeprefix(PLAN_MARKER).strip()
        if "<summary>Implementation notes</summary>" not in CONTEXT["plan"]:
            raise SystemExit("plan: the rendered plan lacks the implementation notes")
        print(CONTEXT["plan"], flush=True)

        implement = stage("implement", work, root / "implement", args.claude)
        if "calc.py" not in implement["changed_files"]:
            raise SystemExit("implement: calc.py was not changed")
        body = pull_body({"issue": 1}, implement)
        if "## What changed" not in body or "## How it is tested" not in body:
            raise SystemExit(f"implement: incomplete pull request description:\n{body}")
        print(body, flush=True)

        # Publish from a fresh checkout, as the pipeline's publish job does, then review it.
        published = root / "published"
        git(root, "clone", "-q", str(root / "origin"), str(published))
        git(published, "checkout", "-q", "-b", "nexkit/issue-1")
        git(published, "apply", "--index", str(root / "implement" / "changes.patch"))
        git(published, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fix")
        work = published
        test = subprocess.run(
            [sys.executable, "-m", "unittest"], cwd=work, capture_output=True, text=True
        )
        checks = [
            {
                "name": "test",
                "run": "python3 -m unittest -v",
                "exit_code": test.returncode,
                "passed": test.returncode == 0,
                "output": test.stderr[-2000:],
            }
        ]
        if not checks[0]["passed"]:
            raise SystemExit("implement: the published change fails its tests")
        review = stage("review", work, root / "review", args.claude, checks=checks, base="main")
        if review["output"]["verdict"] not in ("approve", "request_changes"):
            raise SystemExit("review: no verdict")
        if not review["output"]["criteria"]:
            raise SystemExit("review: no acceptance criteria assessed")
        print(review_body(review, checks), flush=True)

        # A fix round, then a second review that gets the previous round as input.
        reviewed = git(work, "rev-parse", "HEAD")
        (work / "test_calc.py").write_text(
            (work / "test_calc.py").read_text()
            + "\n    def test_add_negative(self):\n        self.assertEqual(add(-2, -3), -5)\n"
        )
        git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fix")
        state = empty_state(1)
        state["last_review"] = {
            "round": 1,
            "head": reviewed,
            "verdict": "request_changes",
            "findings": [
                {
                    "severity": "blocking",
                    "file": "test_calc.py",
                    "line": 0,
                    "body": "No test covers negative numbers.",
                },
                {
                    "severity": "suggestion",
                    "file": "calc.py",
                    "line": 1,
                    "body": "Rename add() to add_numbers().",
                },
            ],
        }
        decision = {"action": "fix", "pr": 2, "auto": False, "note": "Keep the name add()."}
        fix = {
            "status": "done",
            "summary": "Added test_add_negative. Kept the name add(), as the note asks.",
        }
        context = {**CONTEXT, "pr": 2, **previous_review(StateComments(state), decision, fix)}
        second = stage(
            "review", work, root / "second", args.claude, context, checks=checks, base="main"
        )
        previous = second["output"]["previous_findings"]
        print(f"previous findings: {json.dumps(previous)}", flush=True)
        if not previous:
            raise SystemExit("second review: previous findings were not assessed")
        resolutions = {"resolved", "unresolved", "rejection_accepted"}
        if any(item["resolution"] not in resolutions for item in previous):
            raise SystemExit(f"second review: bad resolution in {previous}")
        if {item["severity"] for item in previous} != {"blocking", "suggestion"}:
            raise SystemExit(f"second review: severities not kept in {previous}")
        conflicts(root, args.claude)
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
