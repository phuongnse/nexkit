import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from nexkit import cli
from nexkit.config import CONFIG_PATH
from nexkit.report import READY
from nexkit.state import PLAN_MARKER, profile_record, render_run
from nexkit.status import collect, render
from nexkit.usage import iso, shown
from tests.support import APP, CLAUDE, FakeGitHub, make_config


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        for number in (1, 2, 3, 4, 5, 7, 27):
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

    def merged(self, number, issue, at, by="nexkit-acme[bot]"):
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
        # `/nexkit go` paused at its pull request's review, which carries on there.
        self.command(7, "go", "continued", outcome="paused", attention="usage limit")
        self.gh.add_pull(18, 7)
        self.round(18, 1, status="continued", outcome="paused")
        self.command(27, "plan", "continued", outcome="paused")  # resumed, not started yet
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
            {
                1: "planning",
                2: "planned",
                3: "implementing",
                4: "failed",
                5: "in_pull_request",
                7: "in_pull_request",
                27: "planning",
            },
        )
        self.assertEqual(issues[2]["plan"]["profile"], "hard")
        self.assertEqual(issues[5]["pull"], 6)
        pulls = {p["number"]: p for p in data["pull_requests"]}
        self.assertEqual(
            {n: p["state"] for n, p in pulls.items()},
            {
                6: "ready",
                8: "conflicting",
                10: "running",
                12: "paused",
                14: "ready",
                16: "ready",
                18: "running",
            },
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
                (
                    8,
                    "comment `/nexkit fix` on it, with any rule the conflict needs. It conflicts "
                    "with `main`, and with `conflicts.auto_resolve` off, NexKit starts no round "
                    "to resolve conflicts",
                ),
                (14, READY),
                (16, READY),
            ],
        )
        text = render(data)
        self.assertIn("#2     planned          Issue 2 (profile hard)", text)
        self.assertIn("#10    running", text)
        self.assertIn("round 2 (fix)", text)
        self.assertIn("[ci.yml: success]", text)
        self.assertTrue(text.rstrip().endswith(f"#16    {READY}"))

    def test_a_command_that_waits_to_run_again_after_a_transient_error_needs_nobody(self):
        at = iso(datetime.now(UTC) + timedelta(minutes=30))
        go = {"command": "go", "number": 3, "auto": "retry:2", "at": at, "retry": 2}
        self.command(3, "go", "paused", outcome="retry_waiting", resume=go)
        self.gh.add_pull(8, 9)
        resume = {"command": "review", "number": 8, "auto": "retry", "at": at, "retry": 1}
        self.round(8, 1, status="paused", outcome="retry_waiting", resume=resume)
        cfg = make_config(autopilot={"enabled": True, "scope": None})
        data = collect(self.gh, cfg, workers=1)
        [issue] = [i for i in data["issues"] if i["number"] == 3]
        self.assertEqual((issue["state"], issue["retry_at"]), ("retrying", at))
        [pull] = data["pull_requests"]
        self.assertEqual((pull["state"], pull["retry_at"]), ("retrying", at))
        self.assertEqual(data["needs_person"], [])
        [step] = [s for s in data["autopilot"]["issues"] if s["number"] == 3]
        self.assertEqual(step["state"], "active")
        self.assertEqual(
            step["reason"], f"`/nexkit go` runs again after {shown(at)}, after a transient error"
        )
        text = render(data)
        self.assertIn(f"#3     retrying         Issue 3 (runs again after {at})", text)
        self.assertIn(f"round 1 (fix), runs again after {at}", text)
        # Long past its time with no run started, it waits for a person.
        late = iso(datetime.now(UTC) - timedelta(hours=7))
        self.command(3, "go", "paused", outcome="retry_waiting", resume={**go, "at": late})
        data = collect(self.gh, cfg, workers=1)
        [need] = [n for n in data["needs_person"] if n["number"] == 3]
        self.assertIn("check that NexKit's workflow has its `schedule` trigger", need["reason"])
        self.assertIn("and no run started it", need["reason"])

    def test_conflicts_wait_for_nexkit_with_auto_resolve_conflicts(self):
        self.gh.add_pull(8, 9)
        self.gh.pulls[8]["mergeable"] = False
        self.round(8, 1, outcome="needs_human")
        data = collect(self.gh, make_config(conflicts={"auto_resolve": True}), workers=1)
        self.assertEqual(data["pull_requests"][0]["state"], "conflicting")
        self.assertEqual(data["needs_person"], [])

    def test_a_pull_request_whose_issue_closed_waits_for_a_person_to_merge_it(self):
        self.gh.add_pull(8, 9)
        reason = (
            "the issue closed before NexKit merged its pull request; merge or close it yourself"
        )
        self.round(8, 1, outcome="merge_issue_closed", attention=reason)
        data = collect(self.gh, make_config(merge={"auto": True}), workers=1)
        self.assertEqual(data["pull_requests"][0]["state"], "ready")
        self.assertEqual(
            data["needs_person"], [{"number": 8, "kind": "pull_request", "reason": reason}]
        )
        # NexKit 6.3.0 gave this outcome to a pull request a person merged first.
        self.merged(8, 9, "2026-11-07T07:45:41Z", by="olivia")
        for cfg in (make_config(merge={"auto": True}), make_config(autopilot={"enabled": True})):
            data = collect(self.gh, cfg, workers=1)
            self.assertEqual((data["pull_requests"], data["needs_person"]), ([], []))

    def test_lists_pull_requests_closed_because_the_base_branch_holds_their_change(self):
        for number, issue, outcome in ((30, 27, "already_on_base"), (32, 7, "needs_human")):
            self.gh.add_pull(number, issue, state="closed")
            self.gh.pulls[number].update(closed_at="2026-10-01T00:00:00Z", title=f"PR {number}")
            self.round(number, 1, outcome=outcome, on_base="b" * 40)
        data = collect(self.gh, make_config(), workers=1)
        self.assertEqual(
            data["recent_closed"],
            [
                {
                    "number": 30,
                    "issue": 27,
                    "title": "PR 30",
                    "url": "",
                    "closed_at": "2026-10-01T00:00:00Z",
                    "outcome": "already_on_base",
                    "commit": "b" * 40,
                }
            ],
        )
        self.assertEqual(data["recent_merges"], [])
        self.assertIn(
            "#30    already_on_base  PR 30  (the base branch already holds its change, brought "
            "by bbbbbbb)",
            render(data),
        )

    def test_nothing_to_show(self):
        text = render(collect(self.gh, {}, workers=1))
        self.assertIn("Issues\n  none", text)
        self.assertIn("Needs a person\n  nothing", text)

    def status(self, *flags, repo="/nonexistent"):
        """(exit code, stdout, stderr) of `nexkit status` against the fake GitHub."""
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"GITHUB_TOKEN": "t", "GH_TOKEN": ""}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("nexkit.github.GitHub", return_value=self.gh) as client,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli.main(["status", "--repository", "acme/app", "--repo", str(repo), *flags])
        client.assert_called_once_with(repository="acme/app", token="t")
        # Without the lines that say how far it got.
        err = "".join(
            line for line in stderr.getvalue().splitlines(True) if not line.startswith("status: ")
        )
        return code, stdout.getvalue(), err

    def config(self, **overrides):
        return json.dumps({"agents": CLAUDE, "github_app": APP, **overrides}, indent=2)

    def test_command(self):
        self.gh.files[CONFIG_PATH] = self.config()
        self.command(1, "plan", "running")
        code, out, err = self.status("--json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["issues"][0]["state"], "planning")
        self.assertEqual(
            data["configuration"],
            {"path": CONFIG_PATH, "from": "default_branch", "branch": "main"},
        )

    def test_it_judges_with_the_default_branchs_configuration_as_the_pipeline_does(self):
        # The checkout is a branch from before the App: its config names none, and with it
        # status would count nothing that the App wrote as NexKit's.
        self.gh.files[CONFIG_PATH] = self.config(notify=["olivia"])
        self.command(1, "plan", "running")
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / ".nexkit").mkdir()
            (Path(root) / CONFIG_PATH).write_text(json.dumps({"agents": CLAUDE}))
            code, out, err = self.status(repo=root)
            self.assertEqual(code, 0)
            self.assertIn("#1     planning", out)
            self.assertIn("Configuration: .nexkit/config.json on `main`", out)
            self.assertEqual(
                err.strip(),
                "warning: .nexkit/config.json in this checkout differs from the one on `main`; "
                "status judges with the one on `main`, as the pipeline does (--local-config "
                "uses this checkout's)",
            )
            # The same settings in another layout differ in nothing.
            (Path(root) / CONFIG_PATH).write_text(
                json.dumps(json.loads(self.config(notify=["olivia"])))
            )
            self.assertEqual(self.status(repo=root)[2], "")
            # --local-config previews the checkout's file, and says so.
            (Path(root) / CONFIG_PATH).write_text(self.config(notify=["bob"]))
            code, out, err = self.status("--local-config", repo=root)
            self.assertEqual((code, err), (0, ""))
            self.assertIn(
                "Configuration: .nexkit/config.json in this checkout (--local-config)", out
            )

    def test_without_a_configuration_it_judges_nothing(self):
        cases = [
            (
                None,
                "status judges nothing without a configuration: .nexkit/config.json is not "
                "on `main`; commit it there, or pass --local-config to use this checkout's",
            ),
            ("{", "the .nexkit/config.json on `main` is not valid JSON"),
            ('{"autopilot": {"scope": 3}}', "the .nexkit/config.json on `main` is not valid: "),
        ]
        for text, said in cases:
            with self.subTest(text):
                self.gh.files.pop(CONFIG_PATH, None)
                if text is not None:
                    self.gh.files[CONFIG_PATH] = text
                code, out, err = self.status()
                self.assertEqual((code, out), (1, ""))
                self.assertIn(said, err)
        code, out, err = self.status("--local-config")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Missing .nexkit/config.json", err)


if __name__ == "__main__":
    unittest.main()
