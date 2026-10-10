import contextlib
import io
import json
import os
import unittest
from unittest import mock

from nexkit import cli
from nexkit.state import PLAN_MARKER, profile_record, render_run
from nexkit.status import collect, render
from tests.support import FakeGitHub, make_config


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        for number in (1, 2, 3, 4, 5, 7):
            self.gh.add_issue(number, title=f"Issue {number}")

    def command(self, number, command, status, **run):
        url = f"https://x/runs/{number}{command}"
        self.gh.comment(
            number, render_run({"url": url, "command": command, "status": status, **run})
        )

    def round(self, number, n, status="success", **run):
        row = {
            "round": n,
            "trigger": "fix",
            "url": f"https://x/runs/{number}/{n}",
            "status": status,
        }
        self.gh.comment(number, render_run({**row, **run}))

    def merged(self, number, issue, at, by="github-actions[bot]"):
        self.gh.add_pull(number, issue, state="closed")
        self.gh.pulls[number].update(merged_at=at, merged_by={"login": by}, title=f"PR {number}")

    def test_lists_issues_pull_requests_merges_and_what_needs_a_person(self):
        self.command(1, "plan", "running")
        record = profile_record({"profile": "hard", "chosen_by": "triage"})
        self.gh.comment(2, f"{PLAN_MARKER}\n{record}\n## Plan\n\nDo it.")
        self.command(2, "plan", "success", outcome="planned", attention="the plan has questions")
        self.command(3, "go", "running")
        self.command(
            4, "go", "failure", outcome="agent_error", attention="the run stopped on an error"
        )
        self.command(5, "go", "success", outcome="implemented")
        self.gh.add_pull(6, 5)
        self.round(
            6, 1, outcome="ready", attention="the pull request is ready for a human decision"
        )
        self.gh.add_pull(8, 9)
        self.gh.pulls[8]["mergeable"] = False
        self.round(8, 1, outcome="needs_human", attention="checks or review still need attention")
        self.gh.add_pull(10, 11)
        self.round(10, 2, status="running")
        self.gh.add_pull(12, 13)
        self.round(12, 1, status="paused", outcome="paused", resume={"at": "2026-10-11T04:40:00Z"})
        self.gh.add_pull(14, 15)
        self.round(14, 1, checks="passed", verdict="approve")  # before outcomes were recorded
        self.gh.add_pull(16, 17)
        self.round(16, 1, outcome="ready")
        self.round(16, 2, outcome="up_to_date")
        self.gh.add_pull(30, 31, branch="feature/x")

        self.merged(20, 21, "2026-10-10T08:00:00Z")
        self.round(20, 1, outcome="merged", after_merge=[{"workflow": "ci.yml", "run_id": 1}])
        self.gh.runs[1] = {"status": "completed", "conclusion": "success"}
        self.merged(22, 23, "2026-10-10T09:00:00Z", by="alice")
        self.merged(24, 25, "2026-10-09T08:00:00Z")

        data = collect(self.gh, make_config(), workers=2)
        issues = {i["number"]: i for i in data["issues"]}
        self.assertEqual(
            {n: i["state"] for n, i in issues.items()},
            {1: "planning", 2: "planned", 3: "implementing", 4: "failed", 5: "in_pull_request"},
        )
        self.assertEqual(issues[2]["plan"]["profile"], "hard")
        self.assertEqual(issues[5]["pull"], 6)
        pulls = {p["number"]: p for p in data["pull_requests"]}
        self.assertEqual(
            {n: p["state"] for n, p in pulls.items()},
            {6: "ready", 8: "conflicting", 10: "running", 12: "paused", 14: "ready", 16: "ready"},
        )
        self.assertEqual(pulls[10]["round"], 2)
        self.assertEqual(pulls[12]["resume_at"], "2026-10-11T04:40:00Z")
        self.assertEqual(pulls[6]["link"], self.gh.comments(6)[-1]["html_url"])
        self.assertEqual(
            [(m["number"], m["workflows"]) for m in data["recent_merges"]],
            [(20, [{"workflow": "ci.yml", "url": None, "state": "success"}]), (24, [])],
        )
        self.assertEqual(
            [(n["number"], n["reason"]) for n in data["needs_person"]],
            [
                (2, "the plan has questions"),
                (4, "the run stopped on an error"),
                (6, "the pull request is ready for a human decision"),
                (8, "conflicts with `main`; comment `/nexkit fix`"),
                (14, "the pull request is ready for a human decision"),
                (16, "the pull request is ready for a human decision"),
            ],
        )
        text = render(data)
        self.assertIn("#2     planned          Issue 2 (profile hard)", text)
        self.assertIn("#10    running", text)
        self.assertIn("round 2 (fix)", text)
        self.assertIn("[ci.yml: success]", text)
        self.assertTrue(
            text.rstrip().endswith("#16    the pull request is ready for a human decision")
        )

    def test_conflicts_wait_for_nexkit_with_auto_resolve_conflicts(self):
        self.gh.add_pull(8, 9)
        self.gh.pulls[8]["mergeable"] = False
        self.round(8, 1, outcome="needs_human")
        data = collect(self.gh, make_config(auto_resolve_conflicts=True), workers=1)
        self.assertEqual(data["pull_requests"][0]["state"], "conflicting")
        self.assertEqual(data["needs_person"], [])

    def test_nothing_to_show(self):
        text = render(collect(self.gh, {}, workers=1))
        self.assertIn("Issues\n  none", text)
        self.assertIn("Needs a person\n  nothing", text)

    def test_command(self):
        self.command(1, "plan", "running")
        stdout = io.StringIO()
        env = {"GITHUB_TOKEN": "t", "GH_TOKEN": ""}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("nexkit.github.GitHub", return_value=self.gh) as client,
            contextlib.redirect_stdout(stdout),
        ):
            code = cli.main(
                ["status", "--repository", "acme/app", "--json", "--repo", "/nonexistent"]
            )
        self.assertEqual(code, 0)
        client.assert_called_once_with(repository="acme/app", token="t")
        self.assertEqual(json.loads(stdout.getvalue())["issues"][0]["state"], "planning")


if __name__ == "__main__":
    unittest.main()
