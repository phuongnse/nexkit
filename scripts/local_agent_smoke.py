#!/usr/bin/env python3
"""Repeatable LIVE local CLI fixture. Never claims GitHub Actions acceptance."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nexkit.checks import test_count  # noqa: E402
from nexkit.common import file_hash, write_json  # noqa: E402
from nexkit.policy import agent_result, require  # noqa: E402


def invoke(role, model, workspace, output, seconds):
    schema = ROOT / "schemas" / f"{role}.json"
    skill = ROOT / "plugins/nexkit/skills" / f"nexkit-{role}"
    target = workspace / ".agents/skills" / skill.name
    if not target.exists():
        shutil.copytree(skill, target)
    prompt = (
        f"This is an authorized local NexKit CLI fixture. Read and use {target}/SKILL.md. "
        "No network, GitHub actions, commits, pushes, merges or publication. "
        "Authoritative requirement: signed_sum.py must print the mathematical sum of integer "
        "arguments, preserving negative totals and mixed signs; no arguments print 0. "
        "Positive totals must remain correct. The original abs(sum(values)) loses the sign. "
        "Use tools and python3 -B to inspect source and verify the actual CLI and regression. "
        "This is local evidence only, not GitHub Actions acceptance. "
    )
    prompt += (
        "Implement the correction with meaningful regression tests. Demonstrate fail before "
        "and pass after. Do not change policy or skills. "
        if role == "deliver"
        else "Independently review source, git diff, requirement and actual behavior. Do not "
        "modify any files. Verify the new tests detect the original bug by loading the "
        "original function into memory. Report findings and criterion evidence. "
    )
    result_path = output / f"{role}.json"
    events_path = output / f"{role}-events.jsonl"
    start = time.monotonic()
    with events_path.open("w") as events, (output / f"{role}-stderr.log").open("w") as errors:
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "--ephemeral",
                "--sandbox",
                "workspace-write" if role == "deliver" else "read-only",
                "-c",
                'approval_policy="never"',
                "-c",
                "project_doc_max_bytes=0",
                "--model",
                model,
                "-C",
                str(workspace),
                "--json",
                "--output-schema",
                str(schema),
                "-o",
                str(result_path),
                "-",
            ],
            input=prompt,
            text=True,
            stdout=events,
            stderr=errors,
            timeout=seconds,
        )
    require(
        completed.returncode == 0, f"{role} CLI failed; inspect {events_path} and {errors.name}"
    )
    result = agent_result(json.loads(result_path.read_text()), role)
    require(result["status"] == "done", f"{role} reported blocked")
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    commands = [
        e["item"]
        for e in events
        if e.get("type") == "item.completed"
        and e.get("item", {}).get("type") == "command_execution"
    ]
    require(
        any(
            skill.joinpath("SKILL.md").read_text().strip() in c.get("aggregated_output", "")
            for c in commands
        ),
        f"{role} did not observably read the installed skill",
    )
    return {
        "model": model,
        "elapsed_seconds": round(time.monotonic() - start, 2),
        "result": result,
        "tool_commands": len(commands),
        "usage": next((e["usage"] for e in events if e.get("type") == "turn.completed"), None),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implement-model", required=True)
    parser.add_argument("--review-model", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", default="dist/local-agent-smoke")
    args = parser.parse_args()
    require(0 < args.timeout <= 900, "Timeout must be in 1..900 seconds per session")
    output = Path(args.output).resolve()
    require(not output.exists(), "Use a new output directory to preserve prior results")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="nexkit-live-cli-") as directory:
        root = Path(directory)
        (root / "signed_sum.py").write_text(
            "import sys\ndef total(values):\n    return abs(sum(values))\nif __name__ == '__main__':\n    print(total([int(x) for x in sys.argv[1:]]))\n"
        )
        (root / "test_sum.py").write_text(
            "import unittest\nfrom signed_sum import total\nclass TestSum(unittest.TestCase):\n    def test_positive(self):\n        self.assertEqual(total([2, 3]), 5)\n"
        )
        for argv in (
            ["git", "init", "-q", "--initial-branch=main"],
            ["git", "add", "."],
            [
                "git",
                "-c",
                "user.name=NexKit smoke",
                "-c",
                "user.email=smoke@localhost",
                "commit",
                "-qm",
                "Original local fixture",
            ],
        ):
            subprocess.run(argv, cwd=root, check=True, capture_output=True)
        before = subprocess.check_output(
            [sys.executable, "-B", "signed_sum.py", "-2", "-3"], cwd=root, text=True
        ).strip()
        require(before == "5", "Fixture must reproduce the original defect")
        implementation = invoke("deliver", args.implement_model, root, output, args.timeout)
        verification = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-v"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        (output / "verification.log").write_text(verification.stdout + verification.stderr)
        require(
            verification.returncode == 0
            and test_count({"format": "unittest"}, root, verification.stdout + verification.stderr)
            > 0,
            "Actual local verification failed",
        )
        require(
            subprocess.check_output(
                [sys.executable, "-B", "signed_sum.py", "-2", "-3"], cwd=root, text=True
            ).strip()
            == "-5",
            "Real CLI still loses the sign",
        )
        review_skill = ROOT / "plugins/nexkit/skills/nexkit-review"
        shutil.copytree(review_skill, root / ".agents/skills/nexkit-review")
        snapshot = {
            str(p.relative_to(root)): file_hash(p)
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        review = invoke("review", args.review_model, root, output, args.timeout)
        current = {
            str(p.relative_to(root)): file_hash(p)
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        require(current == snapshot, "Reviewer modified the candidate")
        require(review["result"]["verdict"] == "approve", "Independent reviewer did not approve")
        diff = subprocess.check_output(
            ["git", "diff", "--no-ext-diff", "HEAD", "--"], cwd=root, text=True
        )
        (output / "candidate.diff").write_text(diff)
        summary = {
            "kind": "live local Codex CLI; not GitHub Actions",
            "implementation": implementation,
            "review": review,
            "reviewer_unchanged": True,
            "human_interventions_between_sessions": 0,
            "cost": None,
            "published": False,
        }
        write_json(output / "summary.json", summary)
        print(
            json.dumps(
                {"passed": True, "summary": str(output / "summary.json"), "published": False}
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
