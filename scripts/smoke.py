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
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nexkit  # noqa: E402
from nexkit.agent import run_stage  # noqa: E402
from nexkit.config import validate  # noqa: E402
from nexkit.publish import render_plan  # noqa: E402
from nexkit.state import PLAN_MARKER  # noqa: E402

CONFIG = validate({"model": "haiku", "checks": [{"name": "test", "run": "python3 -m unittest -v"}]})
CONTEXT = {
    "issue": 1,
    "pr": "",
    "title": "add() returns the wrong result",
    "body": "calc.add(2, 3) returns -1. It should return 5.",
    "discussion": "No discussion.",
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


def stage(name, work, out, claude, **kw):
    result = run_stage(name, CONTEXT, CONFIG, work, out, claude=claude, **kw)
    summary = {k: result.get(k) for k in ("status", "error", "cost", "changed_files")}
    print(f"{name}: {json.dumps(summary)}", flush=True)
    if result["status"] != "done":
        raise SystemExit(f"{name} stage failed: {result.get('error')}")
    return result


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

        plan = stage("plan", work, root / "plan", args.claude)
        if not plan["output"]["acceptance_criteria"]:
            raise SystemExit("plan: no acceptance criteria")
        CONTEXT["plan"] = render_plan(plan["output"]).removeprefix(PLAN_MARKER).strip()

        implement = stage("implement", work, root / "implement", args.claude)
        if "calc.py" not in implement["changed_files"]:
            raise SystemExit("implement: calc.py was not changed")

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
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
