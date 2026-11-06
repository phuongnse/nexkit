import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nexkit
import nexkit.config
from nexkit import app, cli, maintain, scaffold
from nexkit.globs import branch_matches
from nexkit.runtimes import DECLARED_SECRETS, RUNTIMES, SECRET_SLOTS
from nexkit.state import (
    ACTIONS_BOT,
    BOT_LOGINS,
    by_bot,
    check_fingerprint,
    empty_state,
    render_state,
    setup_fingerprint,
)
from tests.support import APP, CLAUDE, PROFILES, FakeClaude, FakeGitHub, GitRepos, git
from tests.test_agent import CONTEXT, FIX_DONE

ROOT = Path(__file__).resolve().parent.parent


class InitDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(scaffold, "github_app", return_value=(APP, []))
        self.github_app = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_writes_config_and_workflow(self):
        code = cli.main(
            ["init", "--repo", str(self.root), "--check", "test=npm test", "--setup", "npm ci"]
        )
        self.assertEqual(code, 0)
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(cfg["checks"], [{"name": "test", "run": "npm test"}])
        self.assertEqual(cfg["setup"], ["npm ci"])
        workflow = (self.root / ".github/workflows/nexkit.yml").read_text()
        tag = f"v{nexkit.__version__}"
        self.assertIn(f"uses: phuongnse/nexkit/.github/workflows/pipeline.yml@{tag}", workflow)
        self.assertIn(f"nexkit_ref: {tag}", workflow)
        self.assertNotIn("__KIT", workflow)
        self.assertEqual(cli.main(["init", "--repo", str(self.root)]), 2)  # refuses overwrite
        self.assertEqual(cli.main(["init", "--repo", str(self.root), "--force"]), 0)

    def test_init_names_the_people_to_notify(self):
        args = ["init", "--repo", str(self.root), "--notify", "@olivia", "--notify", "alice"]
        self.assertEqual(cli.main([*args, "--notify", "olivia"]), 0)
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(cfg["notify"], ["olivia", "alice"])

    def test_init_names_the_artifacts_of_ci_runs(self):
        args = ["init", "--repo", str(self.root), "--evidence-artifact", "test-results-e2e"]
        self.assertEqual(cli.main(args), 0)
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(cfg["evidence"], {"artifacts": ["test-results-e2e"]})

    def test_init_rejects_bad_check(self):
        self.assertEqual(cli.main(["init", "--repo", str(self.root), "--check", "npm test"]), 2)
        self.assertFalse((self.root / ".nexkit").exists())

    def test_init_sets_each_field_of_a_check(self):
        code = cli.main(
            [
                "init",
                "--repo",
                str(self.root),
                "--check",
                "test=npm test",
                "--check",
                "e2e=scripts/e2e.sh",
                "--check-timeout",
                "e2e=40",
                "--check-slow",
                "e2e",
                "--check-results",
                "e2e=test-results",
                "--check-results",
                "e2e=artifacts/logs",
                "--check-unaffected",
                "e2e=docs/**",
                "--check-unaffected",
                "e2e=web/**",
                "--check-unaffected",
                "e2e=!web/package.json",
            ]
        )
        self.assertEqual(code, 0)
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(
            cfg["checks"],
            [
                {"name": "test", "run": "npm test"},
                {
                    "name": "e2e",
                    "run": "scripts/e2e.sh",
                    "timeout_minutes": 40,
                    "slow": True,
                    "results": ["test-results", "artifacts/logs"],
                    "unaffected_by": ["docs/**", "web/**", "!web/package.json"],
                },
            ],
        )

    def test_init_runs_checks_in_parallel_jobs_and_groups(self):
        code = cli.main(
            [
                "init",
                "--repo",
                str(self.root),
                "--check",
                "build=make",
                "--check",
                "lint=make lint",
                "--check",
                "e2e=scripts/e2e.sh",
                "--parallel-checks",
                "--check-group",
                "build=fast",
                "--check-group",
                "lint=fast",
            ]
        )
        self.assertEqual(code, 0)
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertTrue(cfg["parallel_checks"])
        self.assertEqual([c.get("group") for c in cfg["checks"]], ["fast", "fast", None])

    def test_init_rejects_a_check_field_without_its_check_or_value(self):
        for flags in (
            ["--check-group", "test=fast"],  # without --parallel-checks
            ["--check-group", "e2e=fast", "--parallel-checks"],
            ["--check-slow", "e2e"],
            ["--check-timeout", "test"],
            ["--check-timeout", "test=soon"],
            ["--check-timeout", "test=0"],
            ["--check-results", "test=../outside"],
            ["--check-unaffected", "e2e=docs/**"],
            ["--check-unaffected", "test"],
            ["--check-unaffected", "test=docs/"],
            ["--check-unaffected", "test=!docs/**"],
        ):
            with self.subTest(flags=flags):
                code = cli.main(["init", "--repo", str(self.root), "--check", "test=x", *flags])
                self.assertEqual(code, 2)
                self.assertFalse((self.root / ".nexkit").exists())

    def test_doctor_without_github(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        git(self.root, "init", "-q")
        findings = scaffold.doctor(self.root)
        self.assertIn((True, ".nexkit/config.json is valid"), findings)
        self.assertTrue(any(f"pipeline at v{nexkit.__version__}" in m for ok, m in findings if ok))
        self.assertIn((False, "The 'origin' remote is not a GitHub repository"), findings)
        self.assertEqual(cli.main(["doctor", "--repo", str(self.root)]), 1)

    def test_doctor_rejects_a_path_pattern_that_is_not_a_glob(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["checks"][0]["unaffected_by"] = ["docs/**", "web/[a"]
        path.write_text(json.dumps(raw))
        self.assertIn(
            (False, "checks[0].unaffected_by: 'web/[a' has a `[` without its `]`"),
            scaffold.doctor(self.root),
        )

    def test_doctor_checks_the_workflows_to_start_after_a_merge(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["merge"]["after_workflows"] = [
            "ci.yml",
            {"workflow": "lint.yml", "every": 5},
            {"workflow": "e2e.yml", "every": 30},
        ]
        path.write_text(json.dumps(cfg))
        workflows = self.root / ".github/workflows"
        (workflows / "ci.yml").write_text("on:\n  push:\n  workflow_dispatch:\n")
        (workflows / "lint.yml").write_text("# no workflow_dispatch yet\non: [push]\n")
        findings = scaffold.doctor(self.root)
        self.assertIn(
            (True, "merge.after_workflows: ci.yml can be started after a merge"), findings
        )
        self.assertIn(
            (
                False,
                "merge.after_workflows: lint.yml has no workflow_dispatch trigger, "
                "so NexKit cannot start it after a merge",
            ),
            findings,
        )
        self.assertIn(
            (False, "merge.after_workflows: .github/workflows/e2e.yml does not exist"), findings
        )
        # An entry that waits for no positive number of commits is not valid.
        cfg["merge"]["after_workflows"][1]["every"] = 0
        path.write_text(json.dumps(cfg))
        self.assertIn(
            (False, "merge.after_workflows[1].every must be an integer from 1 to 1000"),
            scaffold.doctor(self.root),
        )

    def after_merge_findings(self, text, branch="main"):
        """What doctor says about ci.yml with `text`, in merge.after_workflows, about a
        merge also starting it."""
        cli.main(["init", "--repo", str(self.root), "--check", "test=true", "--force"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["merge"]["after_workflows"] = ["ci.yml"]
        path.write_text(json.dumps(cfg))
        (self.root / ".github/workflows/ci.yml").write_text(text)
        with mock.patch.object(scaffold, "default_branch_of", return_value=branch):
            findings = scaffold.doctor(self.root)
        return [m for ok, m in findings if not ok and "a merge also starts" in m]

    def test_doctor_warns_of_an_after_merge_workflow_that_a_push_also_starts(self):
        dispatch = "  workflow_dispatch:\n"
        for how, text in (
            ("no branch filter", f"on:\n  push:\n{dispatch}"),
            ("no branch filter", "on: push\n"),
            ("no branch filter", "on: [push, workflow_dispatch]\n"),
            ("no branch filter", "on:\n  - push\n  - workflow_dispatch\n"),
            ("no branch filter", "'on':\n    push:\n        paths: ['src/**']\n"),
            ("'branches' include main", "on:\n  push:\n    branches: [main]\n"),
            ("'branches' include main", "on:\n  push:\n    branches:\n      - 'ma*'\n"),
            ("'branches' include main", "on:\n  push:\n    branches:\n    - main\n"),
            ("'branches' include main", "on:\n  push:\n    branches: ['**', '!dev']\n"),
            ("'branches' include main", "on: {push: {branches: [main]}, pull_request: {}}\n"),
            ("'branches' include main", "on:\n  push: {branches: [main]}\n"),
            ("no branch filter", "on: {push: {}, workflow_dispatch: {}}\n"),
            ("no branch filter", "on:\n- push\n- workflow_dispatch\n"),
            ("'branches' include main", "on:\n  push:\n    'branches': [main]\n"),
            ("no branch filter", "on:\n  push: null\n"),
            (
                "'branches-ignore' leaves main out",
                "on:\n  push:\n    branches-ignore: ['nexkit/**', 'design/**']\n",
            ),
        ):
            [found] = self.after_merge_findings(text)
            self.assertIn(how, found)
            self.assertIn("'push' trigger", found)
            self.assertIn("leaves main out", found)

    def test_doctor_says_nothing_of_an_after_merge_workflow_a_merge_does_not_start(self):
        for text in (
            "on:\n  workflow_dispatch:\n  pull_request:\n    branches: [main]\n",
            "on: workflow_dispatch\n",
            "on:\n  push:\n    branches: [develop, 'release/*']\n",
            "on:\n  push:\n    branches: ['**', '!main']\n",
            "on:\n  push:\n    branches-ignore: [main]\n",
            "on:\n  push:\n    branches-ignore:\n      - 'ma*'\n",
            "on:\n  push:\n    tags: ['v*']\n",
            "on: {pull_request: {}, workflow_dispatch: {}}\n",
            "on: {push: {branches-ignore: [main]}}\n",
            "on: { pull_request: { branches: [push] } }\n",
            "on:\n  push: *filters\n",
            "on:\n  push:\n    'branches': [develop]\n",
            # A comment is no trigger.
            "on:\n  # push:\n  workflow_dispatch:\n",
        ):
            self.assertEqual(self.after_merge_findings(text), [], text)

    def test_the_default_branch_decides_whether_a_merge_starts_it(self):
        text = "on:\n  push:\n    branches: [main]\n"
        self.assertEqual(self.after_merge_findings(text, "trunk"), [])

    def test_a_branch_filter_reads_as_github_reads_it(self):
        for pattern, branch, found in (
            ("main", "main", True),
            ("main", "maintenance", False),
            ("re*", "release", True),
            ("re*", "release/1", False),
            ("re**", "release/1", True),
            ("v[12]", "v2", True),
            ("v[12]", "v3", False),
            ("a.b", "axb", False),
            ("[", "main", False),
        ):
            self.assertEqual(branch_matches(pattern, branch), found, pattern)

    def doctor_online(self, answers):
        """`doctor` with `gh` answering as `answers` says, by part of an argument."""

        def fake_gh(*args):
            if args[:2] == ("auth", "status"):
                return "Logged in"
            for key, value in answers.items():
                if any(key in arg for arg in args):
                    return value
            return None

        with (
            mock.patch.object(scaffold, "repository_of", return_value="acme/app"),
            mock.patch.object(scaffold, "_gh", side_effect=fake_gh),
        ):
            return scaffold.doctor(self.root)

    def label_findings(self, autopilot, answers):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true", "--force"])
        path = self.root / ".nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["autopilot"] = autopilot
        path.write_text(json.dumps(raw))
        return [
            (ok, message) for ok, message in self.doctor_online(answers) if "autopilot." in message
        ]

    def test_doctor_warns_of_a_label_that_the_repository_lacks(self):
        settings = {"labels": {"run last": {"wave": 1}}, "priority": ["bug", "nexkit:ci"]}
        found = self.label_findings(settings, {"labels/bug": "bug\n"})
        self.assertEqual(
            found,
            [
                (True, "autopilot.priority: acme/app has the label `bug`"),
                (
                    False,
                    "autopilot.labels names `run last`, which acme/app does not have, so no "
                    "issue can carry it: run 'gh label create 'run last' --repo acme/app'",
                ),
            ],
        )

    def test_doctor_warns_of_a_label_that_differs_in_case_since_nexkit_compares_it_exactly(self):
        [(ok, found)] = self.label_findings(
            {"labels": {"last": {"wave": 1}}}, {"labels/last": "Last\n"}
        )
        self.assertFalse(ok)
        self.assertIn("spells the label `Last`", found)

    def test_doctor_names_the_replacement_of_the_label_to_run_last(self):
        found = self.label_findings({"last_in_milestone_label": "last"}, {"labels/last": "last\n"})
        self.assertEqual(
            found,
            [
                (
                    True,
                    "Deprecated: autopilot.last_in_milestone_label still works as a label of "
                    'wave 1, but NexKit 7.0 removes it: in autopilot, replace it with "labels": '
                    '{"last": {"wave": 1}}',
                ),
                (True, "autopilot.last_in_milestone_label: acme/app has the label `last`"),
            ],
        )

    def test_doctor_asks_about_no_label_without_the_setting(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        findings = self.doctor_online({})
        self.assertFalse(any("label" in m for _, m in findings))

    def init_with_label(self, label, answers):
        calls = []

        def fake_gh(*args):
            calls.append(args)
            for key, value in answers.items():
                if any(key in arg for arg in args):
                    return value
            return None

        output = io.StringIO()
        with (
            mock.patch.object(scaffold, "repository_of", return_value="acme/app"),
            mock.patch.object(scaffold, "_gh", side_effect=fake_gh),
            contextlib.redirect_stdout(output),
        ):
            flags = ["--last-in-milestone-label", label, "--app-slug", APP["slug"], "--force"]
            with mock.patch.object(scaffold, "_refuse_without_secrets"):
                code = cli.main(["init", "--repo", str(self.root), "--check", "t=true", *flags])
        self.assertEqual(code, 0)
        raw = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(raw["autopilot"]["labels"], {label: {"wave": 1}})
        return calls, output.getvalue()

    def test_init_creates_the_label_to_run_last_when_it_is_missing(self):
        calls, output = self.init_with_label("run last", {"create": "ok"})
        self.assertIn(("label", "create", "run last", "--repo", "acme/app"), calls)
        self.assertIn("Created the label `run last` on acme/app.", output)

    def test_init_keeps_a_label_to_run_last_that_exists(self):
        calls, output = self.init_with_label("last", {"labels/last": "last\n"})
        self.assertFalse(any(args[:3] == ("label", "create", "last") for args in calls))
        self.assertNotIn("the label `last`", output)

    def test_init_creates_the_labels_of_nexkits_own_issues(self):
        calls, output = self.init_with_label("last", {"labels/last": "last\n", "create": "ok"})
        created = [args[2] for args in calls if args[:2] == ("label", "create")]
        self.assertEqual(created, ["nexkit:ci", "nexkit:dependabot", "nexkit:flaky"])
        self.assertIn(
            "Created the labels `nexkit:ci`, `nexkit:dependabot`, `nexkit:flaky` on acme/app.",
            output,
        )

    def test_init_says_how_to_create_a_label_it_could_not(self):
        _, output = self.init_with_label("last", {})
        self.assertIn("Could not create the label `last`", output)
        self.assertIn("gh label create last --repo acme/app", output)

    def test_init_says_how_to_create_the_label_without_a_github_remote(self):
        cli.main(["init", "--repo", str(self.root), "--check", "t=true", "--force"])
        line = scaffold.ensure_label(self.root, "last")
        self.assertIn("gh label create last", line)

    def test_concurrency_group_is_on_the_job(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".github/workflows/nexkit.yml"
        text = path.read_text()
        group = (
            "concurrency:\n      group: nexkit-${{ (github.event_name == 'milestone' || "
            "(github.event_name == 'issues' && github.event.action != 'closed')) && "
            "'autopilot' || (github.event_name == 'pull_request_review' && "
            "github.event.review.state == 'approved') && format('approved-{0}', "
            "github.event.pull_request.number) || github.event.issue.number || "
            "github.event.pull_request.number || inputs.number || github.event_name }}\n"
        )
        self.assertIn("\n    " + group, text.split("\n  nexkit:\n", 1)[1])
        self.assertNotRegex(text, r"(?m)^concurrency:|cancel-in-progress")
        ok = (True, "Commands on one issue or pull request run one at a time")
        self.assertIn(ok, scaffold.doctor(self.root))
        # A workflow from NexKit 1.7 or earlier sets it for the whole workflow.
        path.write_text(text.replace("    " + group, "").replace("jobs:", "concurrency: x\njobs:"))
        self.assertNotIn(ok, scaffold.doctor(self.root))

    def test_doctor_checks_the_permissions_the_pipeline_needs(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".github/workflows/nexkit.yml"
        text = path.read_text()
        ok = (True, "The workflow gives the pipeline the permissions its jobs need")
        self.assertIn(ok, scaffold.doctor(self.root))
        # A workflow from NexKit 4.3.0 or earlier does not give `checks: read`.
        path.write_text(text.replace("  checks: read\n", ""))
        problem = (
            False,
            ".github/workflows/nexkit.yml does not give the pipeline checks: read, so GitHub "
            "starts none of its runs; add it under 'permissions:' as 'nexkit init' writes it",
        )
        self.assertIn(problem, scaffold.doctor(self.root))
        # Permissions on the job, or all of them, do as well.
        job = "    permissions: write-all\n    uses:"
        path.write_text(text.replace("  checks: read\n", "").replace("    uses:", job))
        self.assertIn(ok, scaffold.doctor(self.root))
        # Reading is all it needs: NexKit writes as its App.
        path.write_text(text.replace("permissions:\n", "permissions: read-all\nunused:\n"))
        self.assertIn(ok, scaffold.doctor(self.root))

    def test_doctor_checks_the_trigger_for_parent_issues(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["close_parent_issues"] = True
        path.write_text(json.dumps(cfg))
        ok = (True, "Closed issues start NexKit, for close_parent_issues")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        text = workflow.read_text()
        workflow.write_text(text.replace("types: [closed, opened,", "types: [opened,"))
        findings = scaffold.doctor(self.root)
        self.assertNotIn(ok, findings)
        self.assertTrue(any("does not listen to 'issues: closed'" in m for _, m in findings))
        cfg["close_parent_issues"] = False
        path.write_text(json.dumps(cfg))
        self.assertFalse(any("issues: closed" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_that_an_approval_reaches_nexkit_with_merge_auto(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["merge"] = {"auto": True}
        path.write_text(json.dumps(cfg))
        ok = (True, "An approval merges a pull request whose merge GitHub refused")
        self.assertIn(ok, scaffold.doctor(self.root))
        # The condition of NexKit 6.1 and earlier lets only *Request changes* through.
        workflow = self.root / ".github/workflows/nexkit.yml"
        text = workflow.read_text()
        old = "github.event.review.state == 'changes_requested' &&"
        new = (
            'contains(fromJSON(\'["changes_requested","approved"]\'), github.event.review.state) &&'
        )
        self.assertIn(new, text)
        workflow.write_text(text.replace(new, old))
        findings = scaffold.doctor(self.root)
        self.assertNotIn(ok, findings)
        self.assertTrue(any("does not let an approval through" in m for k, m in findings if not k))
        # Approvals in the pull request's own group would replace a command that waits.
        workflow.write_text(text.replace("format('approved-{0}', ", "format('{0}', "))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("a group of its own" in m for k, m in findings if not k))
        cfg["merge"] = {"auto": False}
        path.write_text(json.dumps(cfg))
        self.assertFalse(any("approval" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_the_schedule_that_resumes_paused_runs(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        ok = (True, "A schedule resumes runs paused at the agent's usage limit")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        text = workflow.read_text()
        workflow.write_text(re.sub(r"  schedule:\n.*\n", "", text))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("has no 'schedule' trigger" in m for ok, m in findings if not ok))
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["usage_limit"] = {"resume": False}
        path.write_text(json.dumps(cfg))
        self.assertFalse(any("schedule" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_the_triggers_and_group_of_autopilot(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["autopilot"] = {"enabled": True}
        path.write_text(json.dumps(cfg))
        ok = (True, "Issue and milestone events start autopilot sweeps")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        text = workflow.read_text()
        workflow.write_text(text.replace(", milestoned,", ","))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("does not listen to 'milestoned'" in m for ok, m in findings if not ok))
        # Label events count only for `priority` and `labels` of the repository's own.
        old = text.replace(", labeled, unlabeled]", "]")
        workflow.write_text(old)
        self.assertIn(ok, scaffold.doctor(self.root))
        cfg["autopilot"] = {"enabled": True, "labels": {"on-hold": {"hold": True}}}
        path.write_text(json.dumps(cfg))
        findings = scaffold.doctor(self.root)
        self.assertTrue(
            any("'labeled', 'unlabeled'" in m for ok, m in findings if not ok), findings
        )
        cfg["autopilot"] = {"enabled": True}
        path.write_text(json.dumps(cfg))
        workflow.write_text(re.sub(r"  milestone:\n.*\n", "", text))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("'milestone: closed'" in m for ok, m in findings if not ok))
        # With null, milestones change nothing: only issues opening and closing count.
        cfg["autopilot"] = {"enabled": True, "scope": None}
        path.write_text(json.dumps(cfg))
        no_milestones = re.sub(r"  milestone:\n.*\n", "", text).replace(", milestoned,", ",")
        workflow.write_text(no_milestones)
        self.assertIn(ok, scaffold.doctor(self.root))
        for needed in ("opened", "reopened", "closed"):
            workflow.write_text(re.sub(rf"\b{needed}, |, {needed}\b", "", no_milestones, count=1))
            findings = scaffold.doctor(self.root)
            failed = [m for ok, m in findings if not ok and "autopilot" in m]
            self.assertEqual(len(failed), 1, needed)
            self.assertIn(f"does not listen to '{needed}'", failed[0])
        workflow.write_text(re.sub(r"  schedule:\n.*\n", "", no_milestones))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("does not listen to 'schedule'" in m for ok, m in findings if not ok))
        # A workflow from before autopilot: issue events join the issue's group.
        workflow.write_text(text.replace("&& 'autopilot' ", ""))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("the group 'autopilot'" in m for ok, m in findings if not ok))
        cfg["autopilot"] = {"enabled": False}
        path.write_text(json.dumps(cfg))
        self.assertFalse(any("autopilot" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_that_runs_of_the_workflows_after_a_merge_start_sweeps(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["merge"]["after_workflows"] = ["ci.yml", "e2e.yml"]
        path.write_text(json.dumps(cfg))
        workflows = self.root / ".github/workflows"
        ci = 'name: "CI tests" # the checks\non: [workflow_dispatch]\n'
        (workflows / "ci.yml").write_text(ci)
        (workflows / "e2e.yml").write_text("on: [workflow_dispatch]\n")
        workflow = workflows / "nexkit.yml"
        text = workflow.read_text()
        # Without autopilot, no sweep acts on those runs.
        self.assertFalse(any("workflow_run" in m for _, m in scaffold.doctor(self.root)))
        cfg["autopilot"] = {"enabled": True}
        path.write_text(json.dumps(cfg))
        # The workflow `nexkit init` writes shows the trigger in a comment only.
        failed = [m for ok, m in scaffold.doctor(self.root) if not ok and "workflow_run" in m]
        self.assertEqual(len(failed), 1)
        self.assertIn("does not name 'CI tests', '.github/workflows/e2e.yml'", failed[0])
        self.assertIn("the end of the next NexKit run or the hourly schedule", failed[0])
        listened = text.replace(
            "  workflow_dispatch:\n",
            "  workflow_run:\n    workflows: [CI tests, .github/workflows/e2e.yml]\n"
            "    types: [completed]\n    branches: [main]\n  workflow_dispatch:\n",
            1,
        )
        workflow.write_text(listened)
        ok = (True, "Finished runs of merge.after_workflows on the default branch start sweeps")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow.write_text(listened.replace("[completed]", "[requested]"))
        failed = [m for ok, m in scaffold.doctor(self.root) if not ok and "workflow_run" in m]
        self.assertIn("does not listen to 'completed'", failed[0])
        # A workflow from before: the job lets no workflow_run event through.
        workflow.write_text(listened.replace(',"workflow_run"]', "]"))
        failed = [m for ok, m in scaffold.doctor(self.root) if not ok and "workflow_run" in m]
        self.assertIn("does not let 'workflow_run' events through its 'if'", failed[0])

    def test_init_offers_the_trigger_and_the_job_for_workflows_after_a_merge(self):
        workflows = self.root / ".github/workflows"
        workflows.mkdir(parents=True)
        ci = "name: CI tests\non: [push, workflow_dispatch]\njobs:\n  test:\n    runs-on: x\n"
        (workflows / "ci.yml").write_text(ci)
        out = io.StringIO()
        args = ["init", "--repo", str(self.root), "--check", "test=true"]
        with contextlib.redirect_stdout(out):
            cli.main([*args, "--after-workflow", "ci.yml"])
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(cfg["merge"]["after_workflows"], ["ci.yml"])
        workflow = (workflows / "nexkit.yml").read_text()
        self.assertIn('workflows: ["CI tests"]', scaffold.trigger(workflow, "workflow_run"))
        # A finished run sends `workflow_run`, also one that NexKit's App started.
        self.assertNotIn("nexkit-sweep", out.getvalue())
        cfg["autopilot"] = {"enabled": True}
        (self.root / ".nexkit/config.json").write_text(json.dumps(cfg))
        self.assertIn(
            (True, "Finished runs of merge.after_workflows on the default branch start sweeps"),
            scaffold.doctor(self.root),
        )

    def test_init_and_doctor_with_workflows_that_autopilot_only_watches(self):
        workflows = self.root / ".github/workflows"
        workflows.mkdir(parents=True)
        nightly = (
            "name: Nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n"
            "  test:\n    runs-on: x\n"
        )
        (workflows / "nightly.yml").write_text(nightly)
        (workflows / "codeql.yml").write_text("on: [workflow_call]\njobs: {}\n")
        out = io.StringIO()
        args = ["init", "--repo", str(self.root), "--check", "test=true"]
        with contextlib.redirect_stdout(out):
            cli.main([*args, "--watch-workflow", "nightly.yml", "--watch-workflow", "codeql.yml"])
        cfg = json.loads((self.root / ".nexkit/config.json").read_text())
        self.assertEqual(cfg["autopilot"], {"watch_workflows": ["nightly.yml", "codeql.yml"]})
        self.assertNotIn("after_workflows", cfg["merge"])
        workflow = (workflows / "nexkit.yml").read_text()
        found = scaffold.trigger(workflow, "workflow_run")
        self.assertIn('workflows: ["Nightly", ".github/workflows/codeql.yml"]', found)
        # NexKit starts none of them, so they need no job that starts a sweep.
        self.assertNotIn("add this job", out.getvalue())
        cfg["autopilot"]["enabled"] = True
        (self.root / ".nexkit/config.json").write_text(json.dumps(cfg))
        findings = scaffold.doctor(self.root)
        self.assertIn(
            (True, "autopilot.watch_workflows: autopilot watches the runs of nightly.yml"), findings
        )
        self.assertIn(
            (
                False,
                "autopilot.watch_workflows: codeql.yml has no schedule, push or "
                "workflow_dispatch trigger, so nothing runs it on the default branch for "
                "autopilot to watch",
            ),
            findings,
        )
        self.assertIn(
            (
                True,
                "Finished runs of autopilot.watch_workflows on the default branch start sweeps",
            ),
            findings,
        )
        self.assertFalse(any("sweep when" in m or "started after" in m for _, m in findings))
        # Listed in merge.after_workflows too, both name the trigger.
        cfg["merge"]["after_workflows"] = ["ci.yml"]
        (workflows / "ci.yml").write_text("name: CI\non: [workflow_dispatch]\n")
        (self.root / ".nexkit/config.json").write_text(json.dumps(cfg))
        failed = [m for ok, m in scaffold.doctor(self.root) if not ok and "workflow_run" in m]
        self.assertIn(
            "does not name 'CI' of merge.after_workflows and autopilot.watch_workflows", failed[0]
        )
        (workflows / "codeql.yml").unlink()
        self.assertIn(
            (False, "autopilot.watch_workflows: .github/workflows/codeql.yml does not exist"),
            scaffold.doctor(self.root),
        )

    def test_init_listens_to_pushes_to_the_default_branch(self):
        git(self.root, "init", "-q", "-b", "trunk")
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        workflow = (self.root / ".github/workflows/nexkit.yml").read_text()
        self.assertIn("branches: [trunk]", scaffold.trigger(workflow, "push"))
        self.assertEqual(scaffold.default_branch_of(self.root / ".nexkit"), "trunk")

    def test_doctor_checks_the_push_trigger_for_conflicts(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["conflicts"] = {"auto_resolve": True}
        path.write_text(json.dumps(cfg))
        ok = (True, "Pushes to the default branch start the conflict check")
        self.assertIn(ok, scaffold.doctor(self.root))
        workflow = self.root / ".github/workflows/nexkit.yml"
        workflow.write_text(re.sub(r"  push:\n.*\n", "", workflow.read_text()))
        findings = scaffold.doctor(self.root)
        self.assertTrue(any("has no 'push' trigger" in m for ok, m in findings if not ok))

    def test_doctor_checks_that_both_refs_match(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true", "--kit-ref", "v3.0.0"])
        path = self.root / ".github/workflows/nexkit.yml"
        self.assertFalse(any("nexkit_ref" in m for _, m in scaffold.doctor(self.root)))
        # Only `uses:` was upgraded: the jobs would still check out 3.0.0's code.
        text = path.read_text()
        path.write_text(text.replace("pipeline.yml@v3.0.0", "pipeline.yml@v3.1.0"))
        problem = (
            False,
            ".github/workflows/nexkit.yml calls the pipeline at v3.1.0, but nexkit_ref is "
            "v3.0.0; set both to the same tag",
        )
        self.assertIn(problem, scaffold.doctor(self.root))
        path.write_text(text.replace("      nexkit_ref: v3.0.0\n", ""))
        self.assertTrue(
            any("nexkit_ref is not set" in m for ok, m in scaffold.doctor(self.root) if not ok)
        )

    def test_doctor_mentions_a_newer_release(self):
        latest = {"tag": "v3.1.0"}

        def fake_gh(*args):
            if args[:2] == ("auth", "status"):
                return "Logged in"
            if args[:2] == ("api", "repos/phuongnse/nexkit/releases/latest"):
                return latest["tag"] + "\n"
            return None

        def doctor(ref):
            cli.main(["init", "--repo", str(self.root), "--force", "--kit-ref", ref])
            with (
                mock.patch.object(scaffold, "repository_of", return_value="acme/app"),
                mock.patch.object(scaffold, "_gh", side_effect=fake_gh),
            ):
                return [(ok, m) for ok, m in scaffold.doctor(self.root) if "is available" in m]

        # Staying on an older release is a choice: a ✓ that does not fail doctor.
        newer = (
            True,
            "NexKit v3.1.0 is available; this repository uses v3.0.0. To upgrade, see "
            "https://github.com/phuongnse/nexkit/releases/tag/v3.1.0",
        )
        self.assertEqual(doctor("v3.0.0"), [newer])
        self.assertEqual(doctor("v3.1.0"), [])
        self.assertEqual(doctor("main"), [])
        latest["tag"] = "v2.9.0"
        self.assertEqual(doctor("v3.0.0"), [])

    def test_doctor_with_github(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        cfg = json.loads(path.read_text())
        cfg["notify"] = ["owner", "reader"]
        path.write_text(json.dumps(cfg))
        answers = {
            ".default_branch": "main\n",
            "owner/permission": "admin\n",
            "reader/permission": "read\n",
        }

        def fake_gh(*args):
            if args[:2] == ("auth", "status"):
                return "Logged in"
            for key, value in answers.items():
                if any(key in arg for arg in args):
                    return value
            return None

        with (
            mock.patch.object(scaffold, "repository_of", return_value="acme/app"),
            mock.patch.object(scaffold, "_gh", side_effect=fake_gh),
        ):
            findings = scaffold.doctor(self.root)
            self.assertIn(
                (
                    False,
                    "Set a secret for the `claude` agent: run "
                    "'gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo acme/app' with the token "
                    "from 'claude setup-token'",
                ),
                findings,
            )
            self.assertIn((True, "notify: @owner has write access"), findings)
            self.assertTrue(any("@reader has no write access" in m for ok, m in findings))
            self.assertFalse(any("does not list the default branch" in m for _, m in findings))
            answers[".default_branch"] = "trunk\n"
            findings = scaffold.doctor(self.root)
            self.assertTrue(any("does not list the default branch trunk" in m for _, m in findings))
            cfg["notify"] = []
            path.write_text(json.dumps(cfg))
            scaffold.doctor(self.root)  # no notify: the rest of the checks still run

    def test_doctor_checks_that_the_workflow_passes_each_agent_secret(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        self.assertFalse(
            any("does not pass it" in m for _, m in scaffold.doctor(self.root)), "init passes all"
        )
        workflow = self.root / ".github/workflows/nexkit.yml"
        inherit = workflow.read_text()
        text = inherit.replace(
            "    secrets: inherit\n",
            "    secrets:\n      NEXKIT_APP_ID: ${{ secrets.NEXKIT_APP_ID }}\n",
        )
        workflow.write_text(text + "# ${{ secrets.ANTHROPIC_API_KEY }} in a comment\n")
        self.assertIn(
            (
                False,
                "The `claude` agent needs CLAUDE_CODE_OAUTH_TOKEN, but "
                ".github/workflows/nexkit.yml does not pass it to the pipeline; add it under "
                "'secrets:' as 'nexkit init' writes it",
            ),
            scaffold.doctor(self.root),
        )
        config = self.root / ".nexkit/config.json"
        team = {"runtime": "claude-code", "provider": "anthropic", "api_key_secret": "TEAM_KEY"}
        raw = {**json.loads(config.read_text()), "agents": {"team": team}}
        config.write_text(json.dumps({**raw, "agent": "team", "model": "sonnet"}))
        passed = "    secrets:\n      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n"
        workflow.write_text(inherit.replace("    secrets: inherit\n", passed))
        self.assertIn(
            (
                False,
                "The `team` agent needs TEAM_KEY, but .github/workflows/nexkit.yml does not "
                "pass it to the pipeline; write 'secrets: inherit' as 'nexkit init' writes it",
            ),
            scaffold.doctor(self.root),
        )
        workflow.write_text(inherit)
        self.assertFalse(any("does not pass it" in m for _, m in scaffold.doctor(self.root)))

    def test_doctor_checks_the_workflows_autopilot_trusts(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["autopilot"] = {"trusted_workflows": ["audit.yml"]}
        path.write_text(json.dumps(raw))
        problem = (
            False,
            "autopilot.trusted_workflows: .github/workflows/audit.yml does not exist",
        )
        self.assertIn(problem, scaffold.doctor(self.root))
        (self.root / ".github/workflows/audit.yml").write_text("on: push\n")
        ok = (True, "autopilot.trusted_workflows: autopilot takes the issues audit.yml opens")
        self.assertIn(ok, scaffold.doctor(self.root))

    def test_doctor_says_whether_a_workflow_saves_the_caches(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        raw = json.loads(path.read_text())

        def found():
            return [(ok, m) for ok, m in scaffold.doctor(self.root) if "saves the caches" in m]

        self.assertEqual(found(), [])  # no `cache`, nothing to save
        entry = {"name": "npm", "paths": ["~/.npm"], "key_files": ["package-lock.json"]}
        path.write_text(json.dumps({**raw, "cache": [entry]}))
        [(ok, message)] = [(ok, m) for ok, m in scaffold.doctor(self.root) if "cache is set" in m]
        self.assertFalse(ok)
        self.assertIn("no workflow saves the caches, so runs start cold", message)
        ci = self.root / ".github/workflows/ci.yml"
        ci.write_text("# - uses: phuongnse/nexkit/cache@v1\non: push\n")
        self.assertFalse(any(ok for ok, _ in found()), "a comment saves nothing")
        ci.write_text("key: nexkit-${{ runner.os }}-npm-${{ hashFiles('x') }}\n")
        [(ok, message)] = found()
        self.assertTrue(ok)
        self.assertIn("in step with `cache` by hand", message)
        ci.write_text("jobs:\n  t:\n    steps:\n      - uses: phuongnse/nexkit/cache@v1.2.3\n")
        self.assertEqual(
            found(), [(True, "A workflow saves the caches with NexKit's cache action")]
        )

    def test_doctor_refuses_a_configuration_without_the_app(self):
        self.github_app.return_value = (None, [])
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        [message] = [m for ok, m in scaffold.doctor(self.root) if "github_app" in m and not ok]
        self.assertIn("github_app is not set: NexKit acts on GitHub as a GitHub App", message)

    def test_doctor_refuses_a_push_token_with_the_way_to_the_app(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        path = self.root / ".nexkit/config.json"
        raw = json.loads(path.read_text())
        path.write_text(json.dumps({**raw, "publish": {"push_token_secret": "X"}}))
        self.assertTrue(
            any("publish.push_token_secret is gone" in m for _, m in scaffold.doctor(self.root))
        )

    def app_findings(self, answers=None, key_file=None, inspected=None, app_id=None, cfg=None):
        cfg = cfg or nexkit.config.validate({"agents": CLAUDE, "github_app": APP})
        answers = answers if answers is not None else {}

        def fake_gh(*args):
            return next((v for k, v in answers.items() if any(k in a for a in args)), None)

        with (
            mock.patch.object(scaffold, "_gh", side_effect=fake_gh),
            mock.patch.object(app, "inspect", **(inspected or {})) as inspect,
        ):
            self.inspect = inspect
            return scaffold._app_on_github(cfg, "acme/app", key_file, app_id)

    def test_doctor_checks_the_secrets_of_the_app(self):
        cfg = nexkit.config.validate({"agents": CLAUDE, "github_app": APP})
        with mock.patch.object(scaffold, "_gh", return_value="User\n"):
            found = scaffold._app_secrets(cfg, {"NEXKIT_APP_ID"}, "acme/app")
        self.assertEqual(found[0], (True, "github_app.app_id_secret: NEXKIT_APP_ID is set"))
        self.assertFalse(found[1][0])
        self.assertIn("gh secret set NEXKIT_APP_PRIVATE_KEY --repo acme/app", found[1][1])
        self.assertIn("https://github.com/settings/apps/nexkit-acme", found[1][1])

    def dependabot_findings(self, answer, status=None):
        cfg = nexkit.config.validate(
            {"agents": CLAUDE, "github_app": APP, "autopilot": {"dependabot": "high"}}
        )
        with (
            mock.patch.object(scaffold, "_gh_answer", return_value=answer),
            mock.patch.object(scaffold, "_gh", return_value=status),
        ):
            return scaffold._dependabot(cfg, "acme/app", "main")

    def test_doctor_says_when_alerts_are_turned_off(self):
        said = '{"message":"Dependabot alerts are disabled for this repository."}'
        [(ok, found)] = self.dependabot_findings((False, said))
        self.assertFalse(ok)
        self.assertIn("alerts are off for acme/app", found)
        self.assertIn("https://github.com/acme/app/settings/security_analysis", found)

    def test_doctor_says_what_the_last_sweep_found_missing(self):
        problem = "The App may not read alerts: grant it Dependabot alerts: read"
        on, missing = self.dependabot_findings((True, "[]"), f"{problem}\n")
        self.assertEqual(on, (True, "autopilot.dependabot: Dependabot alerts are on"))
        self.assertFalse(missing[0])
        self.assertIn(problem, missing[1])

    def test_doctor_cannot_always_tell_and_that_is_no_failure(self):
        [(ok, found)] = self.dependabot_findings((False, "HTTP 403: Must have admin rights"))
        self.assertTrue(ok)
        self.assertIn("could not tell", found)

    def test_doctor_asks_nothing_about_alerts_without_the_setting(self):
        cfg = nexkit.config.validate({"agents": CLAUDE, "github_app": APP})
        self.assertEqual(scaffold._dependabot(cfg, "acme/app", "main"), [])

    def test_only_the_setting_needs_the_permission_for_alerts(self):
        cfg = nexkit.config.validate(
            {"agents": CLAUDE, "github_app": APP, "autopilot": {"dependabot": "high"}}
        )
        self.assertEqual(app.missing(app.PERMISSIONS), [])
        self.assertEqual(app.missing(app.PERMISSIONS, cfg), ["vulnerability_alerts: read"])
        self.assertEqual(app.missing({**app.PERMISSIONS, **app.ALERTS_PERMISSION}, cfg), [])
        # An App that `nexkit init` creates does without it.
        self.assertNotIn("vulnerability_alerts", app.PERMISSIONS)

    def test_with_its_key_doctor_checks_the_permission_for_alerts(self):
        cfg = nexkit.config.validate(
            {"agents": CLAUDE, "github_app": APP, "autopilot": {"dependabot": "high"}}
        )
        state = {
            "slug": "nexkit-acme",
            "granted": app.PERMISSIONS,
            "installed": True,
            "permissions": app.PERMISSIONS,
        }
        found = self.app_findings({}, "key.pem", {"return_value": state}, app_id="7", cfg=cfg)
        self.assertTrue(any(not ok and "vulnerability_alerts" in m for ok, m in found))

    def test_doctor_says_which_permissions_a_public_app_lacks(self):
        granted = {**app.PERMISSIONS, "workflows": "read"}
        del granted["statuses"]
        answers = {"apps/nexkit-acme": json.dumps({"id": 7, "permissions": granted})}
        [lacking, unknown] = self.app_findings(answers)
        self.assertFalse(lacking[0])
        self.assertIn("lacks statuses: write, workflows: write", lacking[1])
        self.assertIn("https://github.com/settings/apps/nexkit-acme/permissions", lacking[1])
        # Only the App may ask where it is installed, and that is no failure.
        self.assertTrue(unknown[0])
        self.assertIn("pass --app-id ID --app-private-key FILE", unknown[1])
        answers[".owner.type"] = "Organization\n"
        self.assertIn("organizations/acme/settings", self.app_findings(answers)[0][1])

    def test_without_its_key_doctor_cannot_read_a_private_app(self):
        [found] = self.app_findings({})
        self.assertTrue(found[0])
        self.assertIn("GitHub shows a private App to no person's token", found[1])
        self.assertIn("--app-id ID --app-private-key FILE", found[1])

    def test_with_its_key_doctor_reads_a_private_app_and_its_installation(self):
        state = {
            "slug": "nexkit-acme",
            "granted": app.PERMISSIONS,
            "installed": False,
            "permissions": {},
        }
        found = self.app_findings({}, "key.pem", {"return_value": state}, app_id="7")
        self.inspect.assert_called_once_with("acme/app", "7", "key.pem")
        self.assertEqual(found[0], (True, "The App nexkit-acme has the permissions NexKit needs"))
        self.assertFalse(found[1][0])
        self.assertIn("is not installed on acme/app", found[1][1])
        self.assertIn("github.com/apps/nexkit-acme/installations/new", found[1][1])
        state = {**state, "installed": True, "permissions": app.PERMISSIONS}
        found = self.app_findings({}, "key.pem", {"return_value": state}, app_id="7")
        self.assertEqual(found[1], (True, "The App nexkit-acme is installed on acme/app"))
        self.assertEqual(len(found), 2)
        state = {**state, "permissions": {**app.PERMISSIONS, "workflows": "read"}}
        found = self.app_findings({}, "key.pem", {"return_value": state}, app_id="7")
        self.assertFalse(found[2][0])
        self.assertIn("installation on acme/app lacks workflows: write", found[2][1])
        lacking = {**state, "granted": {**app.PERMISSIONS, "checks": "none"}}
        found = self.app_findings({}, "key.pem", {"return_value": lacking}, app_id="7")
        self.assertFalse(found[0][0])
        self.assertIn("lacks checks: read", found[0][1])
        other = {**state, "slug": "someone-else"}
        [found] = self.app_findings({}, "key.pem", {"return_value": other}, app_id="7")
        self.assertFalse(found[0])
        self.assertIn("belong to the App someone-else", found[1])
        wrong = {"side_effect": app.AppError("not an App's private key")}
        found = self.app_findings({}, "key.pem", wrong, app_id="7")
        self.assertEqual(found, [(False, "not an App's private key")])

    def test_doctor_needs_the_id_and_the_key_together(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["doctor", "--repo", str(self.root), "--app-private-key", "k.pem"])
        self.assertEqual(code, 2)
        self.assertIn("go together", err.getvalue())

    def test_init_refuses_before_it_creates_an_app(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        self.github_app.reset_mock()
        self.assertEqual(cli.main(["init", "--repo", str(self.root)]), 2)
        self.github_app.assert_not_called()

    def test_init_says_what_stopped_the_app(self):
        self.github_app.side_effect = app.AppError("not installed")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["init", "--repo", str(self.root)]), 1)
        self.assertIn("not installed", err.getvalue())
        self.assertFalse((self.root / ".nexkit/config.json").exists())

    def test_init_writes_the_app_and_says_what_is_left(self):
        self.github_app.return_value = (APP, ["Install it: https://x"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        self.assertEqual(
            json.loads((self.root / ".nexkit/config.json").read_text())["github_app"], APP
        )
        self.assertIn("Install it: https://x", out.getvalue())
        workflow = (self.root / ".github/workflows/nexkit.yml").read_text()
        self.assertEqual(scaffold.granted(workflow)["contents"], "read")
        self.assertNotIn("write", scaffold.granted(workflow).values())

    def test_doctor_finds_the_secret_of_each_agent(self):
        cfg = nexkit.config.validate(
            {
                "agents": {
                    **CLAUDE,
                    "backup": {
                        "runtime": "claude-code",
                        "provider": "anthropic",
                        "api_key_secret": "ANTHROPIC_API_KEY",
                    },
                },
                "stages": {"review": {"agent": "backup", "model": "opus"}},
            }
        )
        self.assertEqual(
            scaffold._secrets_set(cfg, {"ANTHROPIC_API_KEY", "OTHER"}, "acme/app"),
            [
                (
                    False,
                    "Set a secret for the `claude` agent: run 'gh secret set "
                    "CLAUDE_CODE_OAUTH_TOKEN --repo acme/app' with the token from "
                    "'claude setup-token'",
                ),
                (True, "Secret for the `backup` agent is set: ANTHROPIC_API_KEY"),
            ],
        )

    def test_trigger(self):
        workflow = (
            "on:\n  push:\n    branches: [main] # x\n  schedule:\n    - cron: '1 * * * *'\njobs:\n"
        )
        self.assertIn("branches: [main]", scaffold.trigger(workflow, "push"))
        self.assertIsNotNone(scaffold.trigger(workflow, "schedule"))
        self.assertIsNone(scaffold.trigger(workflow, "issues"))
        self.assertIsNone(scaffold.trigger("on:\n  # issues:\njobs:\n", "issues"))

    def test_repository_of(self):
        git(self.root, "init", "-q")
        git(self.root, "remote", "add", "origin", "git@github.com:acme/app.git")
        self.assertEqual(scaffold.repository_of(self.root), "acme/app")


class PipelineCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root / "output.txt"
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        repo = str(self.root / "repo")
        cli.main(["init", "--repo", repo, "--check", "test=true", "--app-slug", APP["slug"]])
        patcher = mock.patch.object(cli, "_gh", return_value=self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def outputs(self):
        text = self.output.read_text()
        pattern = r"^(\w+)<<(EOF_\w+)\n(.*?)\n\2$"
        return {key: value for key, _, value in re.findall(pattern, text, re.M | re.S)}

    def run_route(self, event_name, event):
        path = self.root / "event.json"
        path.write_text(json.dumps(event))
        env = {
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(path),
            "GITHUB_OUTPUT": str(self.output),
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cli.main(["route", "--repo", str(self.root / "repo")]), 0)
        return self.outputs()

    def test_route_outputs(self):
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice", "type": "User"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["action"], "implement")
        self.assertNotIn("claude_version", out)
        claude = ["CLAUDE_CODE_OAUTH_TOKEN"]
        self.assertEqual(json.loads(out["agent_secrets"]), claude)
        self.assertEqual(json.loads(out["review_secrets"]), claude)
        self.assertEqual(json.loads(out["decision"])["issue"], 5)
        self.assertEqual(json.loads(out["config"])["checks"][0]["name"], "test")
        self.assertEqual(out["agent_ref"], "main")
        self.assertEqual(out["agent_timeout"], "75")

        self.assertEqual(self.gh.reactions, [(3, "eyes")])
        self.assertEqual(len(self.gh.run_comments(5)), 1)

    def test_route_allows_for_the_setup_timeout(self):
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["check_matrix"])["include"][0]["timeout"], 15 + 30 + 10)
        # The setup timeout counts toward the jobs that run setup.
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["limits"] = {"setup_timeout_minutes": 10}
        path.write_text(json.dumps(raw))
        out = self.run_route("issue_comment", event)
        timeout = json.loads(out["check_matrix"])["include"][0]["timeout"]
        self.assertEqual((out["agent_timeout"], timeout), ("55", 15 + 10 + 10))

    def test_route_runs_the_checks_in_one_verify_job_without_parallel_checks(self):
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(
            json.loads(out["check_matrix"]), {"include": [{"group": "", "timeout": 55}]}
        )
        self.assertEqual(out["parallel_checks"], "false")

    def test_route_gives_each_group_and_each_other_check_a_verify_job(self):
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["parallel_checks"] = True
        raw["checks"] = [
            {"name": "build", "run": "make build", "timeout_minutes": 5, "group": "fast"},
            {"name": "e2e", "run": "make e2e", "timeout_minutes": 40},
            {"name": "lint", "run": "make lint", "timeout_minutes": 5, "group": "fast"},
        ]
        path.write_text(json.dumps(raw))
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        matrix = json.loads(out["check_matrix"])["include"]
        # Each job allows for setup and its own checks, not for the others'.
        self.assertEqual(
            matrix,
            [
                {"group": "fast", "timeout": 5 + 5 + 30 + 10},
                {"group": "e2e", "timeout": 40 + 30 + 10},
            ],
        )
        self.assertEqual(out["parallel_checks"], "true")

    def test_route_leaves_out_and_shows_the_checks_whose_paths_did_not_change(self):
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["parallel_checks"] = True
        raw["checks"] = [
            {"name": "unit", "run": "make unit"},
            {"name": "e2e", "run": "make e2e", "unaffected_by": ["docs/**"]},
        ]
        path.write_text(json.dumps(raw))
        passed, head = "a" * 40, "e" * 40
        self.gh.add_pull(6, 5, head_sha=head)
        fingerprints = {name: check_fingerprint(f"make {name}") for name in ("unit", "e2e")}
        self.gh.round_comment(
            6, 1, passed, "passed", passed=fingerprints, setup=setup_fingerprint(raw["setup"])
        )
        self.gh.compares[(passed, head)] = {"status": "ahead", "files": [{"filename": "docs/a"}]}
        inputs = {"command": "review", "number": "6", "note": "", "auto": "true,carry"}
        event = {"inputs": inputs, "sender": {"login": "nexkit-acme[bot]"}}
        summary = self.root / "summary.md"
        stdout = io.StringIO()
        env = {
            "GITHUB_REPOSITORY": "acme/app",
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_RUN_ID": "99",
        }
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
            out = self.run_route("workflow_dispatch", event)
        line = "`e2e` skipped: no path it reads changed since `aaaaaaa`."
        self.assertEqual(json.loads(out["check_matrix"])["include"][0]["group"], "unit")
        self.assertEqual(len(json.loads(out["check_matrix"])["include"]), 1)
        self.assertEqual(out["skip_verify"], "false")
        self.assertEqual(
            json.loads(out["decision"])["skipped_checks"], [{"name": "e2e", "since": passed}]
        )
        self.assertIn(f"::notice title=NexKit skipped a check::{line}", stdout.getvalue())
        self.assertIn(f"### NexKit checks: skipped\n\n- {line}", summary.read_text())
        # Every check unchanged: no verify job runs.
        self.gh.compares[(passed, head)] = {"status": "identical", "files": []}
        raw["checks"][0]["unaffected_by"] = ["**"]
        path.write_text(json.dumps(raw))
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            out = self.run_route("workflow_dispatch", event)
        self.assertEqual(out["skip_verify"], "true")
        self.assertEqual(len(json.loads(out["check_matrix"])["include"]), 2)

    def test_route_allows_for_triage_and_the_longest_plan(self):
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw.update(profiles=PROFILES, default_profile="standard")
        path.write_text(json.dumps(raw))
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit plan", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["agent_timeout"], str(5 + 40 + 30))
        self.assertIsNone(json.loads(out["decision"])["previous_profile"])
        # Other runs get the profile's settings in the configuration they pass on.
        event["comment"]["body"] = "/nexkit go"
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["decision"])["profile"], "standard")
        cfg = json.loads(out["config"])
        self.assertEqual(nexkit.config.stage(cfg, "implement")["model"], "sonnet")

    def test_route_allows_for_and_passes_the_secrets_of_fallback_agents(self):
        from tests.test_fallback import AGENTS

        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw.update(agents=AGENTS, stages={"implement": {"timeout_minutes": 40}})
        path.write_text(json.dumps(raw))
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit go", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        # Each of the three agents may take the stage's timeout, then setup.
        self.assertEqual(out["agent_timeout"], str(3 * 40 + 30))
        self.assertEqual(
            json.loads(out["agent_secrets"]),
            ["CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_SPARE_TOKEN"],
        )

    def test_route_passes_each_job_the_secrets_of_its_agents(self):
        from tests.test_runtimes import MultiAgentTests

        path = self.root / "repo/.nexkit/config.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **MultiAgentTests.RAW}))
        claude = ["CLAUDE_CODE_OAUTH_TOKEN"]
        team = ["TEAM_KEY"]
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {"id": 3, "body": "/nexkit plan", "user": {"login": "alice"}},
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["agent_secrets"]), sorted([*claude, *team]))
        # Without a plan, go runs on default_profile: implement and review on Claude.
        event["comment"]["body"] = "/nexkit go"
        out = self.run_route("issue_comment", event)
        self.assertEqual(json.loads(out["agent_secrets"]), claude)
        self.assertEqual(json.loads(out["review_secrets"]), claude)

    def test_route_on_pull_request_marks_the_round_running(self):
        self.gh.add_pull(6, 5, head_sha="b" * 40)
        event = {
            "action": "created",
            "issue": {"number": 6, "pull_request": {}},
            "comment": {"id": 4, "body": "/nexkit review", "user": {"login": "alice"}},
        }
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "9"}):
            out = self.run_route("issue_comment", event)
        self.assertEqual(out["action"], "review")
        self.assertEqual(self.gh.reactions, [(4, "eyes")])
        self.assertEqual({s["state"] for s in self.gh.statuses}, {"pending"})
        self.assertTrue(self.gh.comments_matching(6, "actions/runs/9"))

    def test_a_vouch_with_autopilot_off_says_so(self):
        self.gh.add_issue(7, user="bob")
        event = {
            "action": "created",
            "issue": {"number": 7},
            "comment": {"id": 9, "body": "/nexkit trust", "user": {"login": "alice"}},
        }
        self.assertEqual(self.run_route("issue_comment", event)["action"], "none")
        [reply] = self.gh.comments(7)
        self.assertIn("autopilot.enabled off", reply["body"])

    def test_route_hands_closed_issues_to_maintain_only_when_enabled(self):
        event = {"action": "closed", "issue": {"number": 5}}
        out = self.run_route("issues", event)
        self.assertEqual(out["action"], "none")
        self.assertFalse(self.gh.issue_comments[5])
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["close_parent_issues"] = True
        path.write_text(json.dumps(raw))
        out = self.run_route("issues", event)
        self.assertEqual(out["action"], "maintain")
        self.assertEqual(json.loads(out["decision"])["task"], "parents")
        self.assertEqual(out["maintain_timeout"], "15")
        self.assertFalse(self.gh.issue_comments[5])  # no run comment, no reaction

    def test_a_refused_command_that_autopilot_started_starts_a_sweep_after_the_run(self):
        # `route` refuses a `/nexkit go` that autopilot started: the issue's pull request
        # is open. A sweep that waits for this run to end finds the command lost.
        self.gh.add_pull(6, 5)
        inputs = {"command": "go", "number": "5", "note": "", "auto": "autopilot"}
        event = {"inputs": inputs, "sender": {"login": "nexkit-acme[bot]"}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "81"}):
            out = self.run_route("workflow_dispatch", event)
        self.assertEqual(out["action"], "none")
        self.assertTrue(self.gh.comments_matching(5, "Pull request #6 is already open"))
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["autopilot"] = {"enabled": True}
        path.write_text(json.dumps(raw))
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "81"}):
            out = self.run_route("workflow_dispatch", event)
        decision = json.loads(out["decision"])
        self.assertEqual(out["action"], "maintain")
        self.assertEqual((decision["task"], decision["after"]), ("lost", 81))
        cfg = json.loads(out["config"])
        lines = maintain.maintain(self.gh, decision, cfg, workflow="nexkit.yml", ref="main")
        self.assertEqual(lines, ["Started an autopilot sweep."])
        [sweep] = [d["inputs"] for d in self.gh.dispatches]
        self.assertEqual(sweep["note"], "after run 81")
        # A person's command that route refuses starts nothing.
        self.gh.dispatches.clear()
        event = {"inputs": {**inputs, "auto": "false"}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "82"}):
            self.assertEqual(self.run_route("workflow_dispatch", event)["action"], "none")

    def test_route_hands_autopilot_events_to_maintain_only_when_enabled(self):
        events = [
            ("issues", {"action": "opened", "issue": {"number": 5}}),
            ("issues", {"action": "milestoned", "issue": {"number": 5}}),
            ("milestone", {"action": "closed", "milestone": {"number": 1}}),
            ("workflow_dispatch", {"inputs": {"command": "autopilot", "number": "autopilot"}}),
        ]
        for name, event in events:
            self.assertEqual(self.run_route(name, event)["action"], "none")
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["autopilot"] = {"enabled": True}
        path.write_text(json.dumps(raw))
        decisions = [json.loads(self.run_route(name, event)["decision"]) for name, event in events]
        self.assertEqual(
            decisions,
            [
                {"action": "maintain", "task": "autopilot", "event": "opened"},
                {"action": "maintain", "task": "autopilot", "event": "milestoned"},
                {"action": "maintain", "task": "autopilot", "event": "milestone_closed"},
                {"action": "maintain", "task": "sweep"},
            ],
        )
        event = {"inputs": {"command": "autopilot", "number": "autopilot", "note": "7"}}
        decision = json.loads(self.run_route("workflow_dispatch", event)["decision"])
        self.assertEqual(decision, {"action": "maintain", "task": "sweep", "closed": 7})
        self.assertFalse(self.gh.issue_comments[5])  # no run comment, no reaction

    def test_route_hands_finished_runs_and_sweeps_after_a_run_to_maintain(self):
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        raw["autopilot"] = {"enabled": True}
        path.write_text(json.dumps(raw))
        run = {"id": 31, "path": ".github/workflows/ci.yml"}
        decision = json.loads(
            self.run_route("workflow_run", {"action": "completed", "workflow_run": run})["decision"]
        )
        self.assertEqual(
            decision, {"action": "maintain", "task": "ci", "workflow": "ci.yml", "run": 31}
        )
        out = self.run_route("workflow_run", {"action": "requested", "workflow_run": run})
        self.assertEqual(out["action"], "none")
        event = {"inputs": {"command": "autopilot", "number": "autopilot", "note": "after run 9"}}
        decision = json.loads(self.run_route("workflow_dispatch", event)["decision"])
        self.assertEqual(decision, {"action": "maintain", "task": "sweep", "after": 9})

    def test_route_gives_the_waiting_run_its_wait(self):
        event = {"inputs": {"command": "resume", "number": "resume"}}
        out = self.run_route("workflow_dispatch", event)
        self.assertEqual(json.loads(out["decision"]), {"action": "maintain", "task": "resume"})
        self.assertEqual(out["maintain_timeout"], str(330 + 15))

    def test_route_stops_when_the_key_belongs_to_another_app(self):
        with mock.patch.dict(os.environ, {"NEXKIT_APP_SLUG": "someone-else"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                with self.assertRaises(AssertionError):
                    self.run_route("schedule", {})
        self.assertIn("::error::github_app.slug is nexkit-acme", out.getvalue())
        self.assertIn("belongs to someone-else", out.getvalue())

    def test_route_counts_only_the_comments_of_the_app_as_nexkits(self):
        self.addCleanup(BOT_LOGINS.update, set(BOT_LOGINS))
        self.addCleanup(BOT_LOGINS.clear)
        BOT_LOGINS.clear()
        BOT_LOGINS.add(ACTIONS_BOT)
        self.run_route("schedule", {})
        self.assertTrue(by_bot({"user": {"login": "nexkit-acme[bot]"}}))
        self.assertFalse(by_bot({"user": {"login": ACTIONS_BOT}}))
        self.assertFalse(by_bot({"user": {"login": "other[bot]"}}))

    def test_a_run_without_the_app_stops_at_its_start_and_says_why(self):
        path = self.root / "repo/.nexkit/config.json"
        raw = json.loads(path.read_text())
        del raw["github_app"]
        path.write_text(json.dumps(raw))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["app", "--repo", str(self.root / "repo")])
        self.assertEqual(code, 1)
        self.assertIn("::error::github_app is not set", out.getvalue())

    def test_a_run_names_the_secrets_of_the_app(self):
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output)}):
            self.assertEqual(cli.main(["app", "--repo", str(self.root / "repo")]), 0)
        text = self.output.read_text()
        self.assertIn("app_id_secret<<", text)
        self.assertIn("NEXKIT_APP_PRIVATE_KEY", text)

    def test_a_run_that_got_no_token_says_what_to_check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["app", "--no-token"]), 1)
        self.assertIn("::error::NexKit could not get a token", out.getvalue())

    def test_the_wait_for_paused_commands_only_waits(self):
        env = {
            "NEXKIT_CONFIG": json.dumps({"agents": CLAUDE, "github_app": APP}),
            "NEXKIT_DECISION": "{}",
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(maintain, "wait_for_paused") as wait,
            mock.patch.object(maintain, "maintain") as work,
        ):
            self.assertEqual(cli.main(["maintain", "--wait"]), 0)
        wait.assert_called_once_with(self.gh, 330)
        work.assert_not_called()

    def test_route_replies_with_configuration_errors(self):
        (self.root / "repo/.nexkit/config.json").write_text('{"model": 1}')
        event = {
            "action": "created",
            "issue": {"number": 5},
            "comment": {
                "id": 3,
                "body": "/nexkit plan",
                "user": {"login": "alice", "type": "User"},
            },
        }
        out = self.run_route("issue_comment", event)
        self.assertEqual(out["action"], "none")
        self.assertTrue(self.gh.comments_matching(5, "configuration error"))

    def test_agent_setup_failure_is_redacted_and_summarised(self):
        setup = 'echo "key $DEPLOY_TOKEN, agent [$ANTHROPIC_API_KEY]"; exit 4'
        cfg = nexkit.config.validate({"agents": CLAUDE, "setup": [setup]})
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text("{}")
        summary = self.root / "summary.md"
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "implement", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(cfg),
            "DEPLOY_TOKEN": "fake-deploy-token-0123456789",
            "ANTHROPIC_API_KEY": "fake-api-key-0123456789",
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
            code = cli.main(["agent", "--repo", str(self.root), "--out", str(out)])
        self.assertEqual(code, 0)
        result = (out / "result.json").read_text()
        self.assertIn("exit code 4", result)
        for text in (stdout.getvalue(), result, summary.read_text()):
            self.assertNotIn("fake-deploy-token-0123456789", text)
            # Setup never sees the agent's credential.
            self.assertIn("key ***, agent []", text)
        self.assertIn("### NexKit implement: error", summary.read_text())

    def test_a_round_on_an_edited_issue_runs_no_setup_and_no_agent(self):
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text(json.dumps({"untrusted": "@bob edited it"}))
        env = {"NEXKIT_DECISION": json.dumps({"action": "implement", "base": "main"})}
        env["NEXKIT_CONFIG"] = (self.root / "repo/.nexkit/config.json").read_text()
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("nexkit.checks.setup") as setup,
            mock.patch("nexkit.agent.run_stage") as run_stage,
            mock.patch("builtins.print"),
        ):
            code = cli.main(["agent", "--repo", str(self.root / "repo"), "--out", str(out)])
        self.assertEqual(code, 0)
        setup.assert_not_called()
        run_stage.assert_not_called()
        result = json.loads((out / "result.json").read_text())
        self.assertEqual((result["status"], result["error"]), ("untrusted", "@bob edited it"))

    def test_agent_that_raises_leaves_a_redacted_error(self):
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text("{}")
        summary = self.root / "summary.md"
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "review", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(nexkit.config.validate({"agents": CLAUDE})),
            "CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth-token-0123456789",
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        error = RuntimeError("git fetch failed for fake-oauth-token-0123456789@github.com")
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("nexkit.agent.run_stage", side_effect=error),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli.main(
                ["agent", "--stage", "review", "--repo", str(self.root), "--out", str(out)]
            )
        self.assertEqual(code, 0)
        result = json.loads((out / "result.json").read_text())
        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"], "NexKit stopped on RuntimeError: git fetch failed for ***@github.com"
        )
        for text in (stdout.getvalue(), summary.read_text()):
            self.assertNotIn("fake-oauth-token-0123456789", text)
        self.assertIn("### NexKit review: error", summary.read_text())

    def test_agent_setup_gets_the_base_commit(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        repo = repos.clone("agent")  # implement starts on the base branch itself
        cfg = nexkit.config.validate(
            {"agents": CLAUDE, "setup": ['echo "base=$NEXKIT_BASE_SHA"; exit 4']}
        )
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text("{}")
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "implement", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(cfg),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
        }
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "--repo", str(repo), "--out", str(out)])
        result = json.loads((out / "result.json").read_text())
        self.assertIn(f"base={git(repo, 'rev-parse', 'HEAD')}", result["error"])

    def test_carry_says_whether_the_approved_review_holds_for_the_new_head(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        approved = repos.push_commit("dev", "nexkit/issue-5", {"calc.py": "x\n"}, "Fix").strip()
        repos.push_commit("base", "main", {"other.py": "y\n"}, "Move main")
        repo = repos.clone("review")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "origin/main")
        out = self.root / "out"
        for approval, carried in ((approved, "true"), ("f" * 40, "false")):
            self.output.unlink(missing_ok=True)
            env = {
                "NEXKIT_DECISION": json.dumps({"base": "main", "carry_from": approval}),
                "GITHUB_OUTPUT": str(self.output),
            }
            shutil.rmtree(out, ignore_errors=True)
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["carry", "--repo", str(repo), "--out", str(out)]), 0)
            self.assertEqual(self.outputs()["carried"], carried)
            record = json.loads((out / "carry.json").read_text())
            self.assertEqual((record["from"], record["carried"]), (approval, carried == "true"))
            if carried == "true":
                result = json.loads((out / "result.json").read_text())
                self.assertEqual((result["status"], result["carried"]), ("carried", approved))
            else:
                # The review agent writes this round's result.
                self.assertFalse((out / "result.json").exists())
                self.assertIn("is not a merge of `main`", record["why"])

    def test_checks_get_the_merge_base_only_when_it_is_known(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        fork = git(repos.root / "seed", "rev-parse", "HEAD")
        repos.push_commit("dev", "nexkit/issue-5", {"a.txt": "a\n"}, "A")
        repos.push_commit("dev", "main", {"b.txt": "b\n"}, "B")
        repo = repos.clone("verify")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        cfg = nexkit.config.validate(
            {
                "agents": CLAUDE,
                "checks": [{"name": "base", "run": 'echo "${NEXKIT_BASE_SHA-unset}"'}],
            }
        )
        for base, seen in (("main", fork), ("gone", "unset")):
            env = {
                "NEXKIT_DECISION": json.dumps({"action": "review", "base": base}),
                "NEXKIT_CONFIG": json.dumps(cfg),
                "NEXKIT_BASE_SHA": "stale",
            }
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
                cli.main(["checks", "--repo", str(repo), "--out", str(self.root / "out")])
            checks = json.loads((self.root / "out" / "checks.json").read_text())
            self.assertEqual(checks[0]["output"].strip(), seen)
        self.assertIn("NEXKIT_BASE_SHA is not set", stdout.getvalue())

    def test_a_fix_round_gets_the_last_commit_whose_checks_passed(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        passed = repos.push_commit("dev", "nexkit/issue-5", {"a.txt": "a\n"}, "Round 1")
        failed = repos.push_commit("dev", "nexkit/issue-5", {"b.txt": "b\n"}, "Round 2")
        self.gh.add_pull(6, 5, head_sha=failed)
        self.gh.round_comment(6, 1, passed, "passed")
        self.gh.round_comment(6, 2, failed, "failed")
        event = {
            "action": "created",
            "issue": {"number": 6, "pull_request": {}},
            "comment": {"id": 3, "body": "/nexkit fix", "user": {"login": "alice"}},
        }
        decision = self.run_route("issue_comment", event)["decision"]
        self.assertEqual(json.loads(decision)["verified_sha"], passed)

        seen = 'echo "${NEXKIT_VERIFIED_SHA-unset}"'
        cfg = nexkit.config.validate(
            {
                "agents": CLAUDE,
                "setup": [f"{seen} > setup.txt"],
                "checks": [{"name": n, "run": f"cat setup.txt; {seen}"} for n in ("unit", "e2e")],
            }
        )
        env = {
            "NEXKIT_DECISION": decision,
            "NEXKIT_CONFIG": json.dumps(cfg),
            "NEXKIT_VERIFIED_SHA": "stale",
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
        }
        # The agent job: setup, then the agent, which may run the checks itself.
        agent_repo = repos.clone("agent")
        git(agent_repo, "checkout", "-q", "nexkit/issue-5")
        out = self.root / "agent-out"
        out.mkdir()
        (out / "context.json").write_text(json.dumps(CONTEXT))
        claude = FakeClaude(self.tmp.name)
        self.addCleanup(claude.restore)
        claude.configure(result=FIX_DONE, write={"calc.py": "# fixed\n"})
        path = f"{claude.dir}{os.pathsep}{os.environ['PATH']}"
        with (
            mock.patch.dict(os.environ, {**env, "PATH": path}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.main(["agent", "--repo", str(agent_repo), "--out", str(out)])
        self.assertEqual(json.loads((out / "result.json").read_text())["status"], "done")
        self.assertEqual((agent_repo / "setup.txt").read_text().strip(), passed)
        self.assertEqual(claude.call()["verified_sha"], passed)

        # The verify job, on the round's new commit.
        verify_repo = repos.clone("verify")
        git(verify_repo, "checkout", "-q", "nexkit/issue-5")
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
            cli.main(["checks", "--repo", str(verify_repo), "--out", str(self.root / "out")])
        checks = json.loads((self.root / "out" / "checks.json").read_text())
        self.assertEqual([c["output"].split() for c in checks], [[passed, passed]] * 2)
        self.assertIn(f"NEXKIT_VERIFIED_SHA={passed}", stdout.getvalue())

    def test_a_fix_round_gets_the_result_files_of_the_failing_checks(self):
        repos = GitRepos()
        self.addCleanup(repos.cleanup)
        head = repos.push_commit("dev", "nexkit/issue-5", {"a.txt": "a\n"}, "Round 1")
        self.gh.add_pull(6, 5, head_sha=head)
        run = (
            "mkdir -p test-results && printf 'select failed ghp_%036d\\n' 0 "
            "> test-results/e2e.log; exit 1"
        )
        cfg = nexkit.config.validate(
            {
                "agents": CLAUDE,
                "checks": [{"name": "e2e", "run": run, "results": ["test-results", "missing"]}],
            }
        )
        quiet = contextlib.redirect_stdout(io.StringIO())
        # The verify job of the round whose checks failed.
        verify_repo = repos.clone("verify")
        git(verify_repo, "checkout", "-q", "nexkit/issue-5")
        artifact = self.root / "verify-out"
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "review", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(cfg),
            "GITHUB_STEP_SUMMARY": str(self.root / "verify-summary.md"),
        }
        with mock.patch.dict(os.environ, env), quiet:
            cli.main(["checks", "--repo", str(verify_repo), "--out", str(artifact)])
        self.assertIn("e2e: missing", (self.root / "verify-summary.md").read_text())
        # Its report keeps the checks and the run that holds their files in the state.
        state = empty_state(5)
        checks = json.loads((artifact / "checks.json").read_text())
        state["feedback"] = {"head": head, "checks": checks, "run_id": 4242}
        self.gh.comment(6, render_state(state))

        event = {
            "action": "created",
            "issue": {"number": 6, "pull_request": {}},
            "comment": {"id": 3, "body": "/nexkit fix", "user": {"login": "alice"}},
        }
        decision = self.run_route("issue_comment", event)["decision"]
        self.assertEqual(json.loads(decision)["results_run"], 4242)
        # The agent job downloads that run's artifact into RUNNER_TEMP, outside the checkout.
        downloaded = self.root / "runner-temp" / "nexkit-checks"
        shutil.copytree(artifact, downloaded)
        out = self.root / "agent-out"
        env = {
            "NEXKIT_DECISION": decision,
            "NEXKIT_CONFIG": json.dumps(cfg),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
        }
        with mock.patch.dict(os.environ, env), quiet:
            cli.main(["context", "--out", str(out), "--results", str(downloaded)])
        agent_repo = repos.clone("agent")
        git(agent_repo, "checkout", "-q", "nexkit/issue-5")
        claude = FakeClaude(self.tmp.name)
        self.addCleanup(claude.restore)
        claude.configure(result=FIX_DONE, write={"calc.py": "# fixed\n"})
        path = f"{claude.dir}{os.pathsep}{os.environ['PATH']}"
        with mock.patch.dict(os.environ, {**env, "PATH": path}), quiet:
            cli.main(["agent", "--repo", str(agent_repo), "--out", str(out)])
        self.assertEqual(json.loads((out / "result.json").read_text())["status"], "done")

        prompt = claude.call()["prompt"]
        folder = (downloaded / "results").resolve()
        self.assertIn(f"NexKit downloaded them to `{folder}`, outside the working tree", prompt)
        self.assertIn(f"- Failing check `e2e` (`{run}`): `{folder}/e2e/`", prompt)
        self.assertIn("  - `test-results/`: a folder with 1 file\n", prompt)
        self.assertNotIn("`missing`", prompt)
        kept = folder / "e2e/test-results/e2e.log"
        self.assertEqual(kept.read_text(), "select failed ***\n")
        self.assertFalse(kept.is_relative_to(agent_repo.resolve()))
        self.assertNotIn("test-results", claude.call()["files"])


class InstallAgentsTests(unittest.TestCase):
    def test_installs_each_runtime_the_config_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "path"
            env = {
                "NEXKIT_CONFIG": json.dumps(nexkit.config.validate({"agents": CLAUDE})),
                "GITHUB_PATH": str(path),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch("subprocess.run") as run,
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(cli.main(["install-agents"]), 0)
            commands, bin_dir = RUNTIMES["claude-code"].install()
            self.assertEqual([c.args[0] for c in run.call_args_list], commands)
            self.assertEqual(run.call_args.kwargs, {"cwd": Path.home(), "check": True})
            self.assertEqual(path.read_text(), f"{bin_dir}\n")
        self.assertIn(f"Installing claude-code {nexkit.CLAUDE_CODE}", stdout.getvalue())
        self.assertEqual(commands[0][-1], nexkit.CLAUDE_CODE)


class SupportedVersionTests(unittest.TestCase):
    """The workflows use the tested versions, and the Claude Code pin has one source."""

    def test_workflows_use_the_tested_versions(self):
        wanted_python = ".".join(map(str, nexkit.PYTHON))
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            self.assertEqual(set(re.findall(r"runs-on: (\S+)", text)), {nexkit.RUNNER}, path.name)
            self.assertLessEqual(
                set(re.findall(r'python-version: "([^"]+)"', text)), {wanted_python}, path.name
            )
            self.assertNotIn("python3 ", text, path.name)
            # Runtime versions are read from nexkit/__init__.py, never repeated.
            for runtime in RUNTIMES.values():
                self.assertNotIn(runtime.version, text, path.name)

    def test_pyproject_declares_the_minimum_python(self):
        text = (ROOT / "pyproject.toml").read_text()
        major, minor = nexkit.PYTHON
        self.assertIn(f'requires-python = ">={major}.{minor}"', text)
        self.assertIn(f'target-version = "py{major}{minor}"', text)

    def test_older_python_versions_are_refused(self):
        self.assertTrue(nexkit.python_supported((*nexkit.PYTHON, 7, "final", 0)))
        self.assertTrue(nexkit.python_supported((3, 14, 0, "final", 0)))
        self.assertFalse(nexkit.python_supported((3, 11, 9, "final", 0)))
        with mock.patch.object(cli.sys, "version_info", (3, 11, 9, "final", 0)):
            self.assertEqual(cli.main(["--version"]), 2)


class WorkflowSafetyTests(unittest.TestCase):
    """Credential separation is the pipeline's core safety property; keep it explicit."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / ".github/workflows/pipeline.yml").read_text()
        body = cls.text.split("\njobs:\n", 1)[1]
        cls.jobs = dict(re.findall(r"^  (\w+):\n((?:    .*\n|\n)*)", body, re.M))
        # What pipeline.yml says in the call of these two, and the job that runs.
        cls.calls = {name: cls.jobs[name] for name in ("agent", "review")}
        for name in cls.calls:
            nested = (ROOT / f".github/workflows/pipeline-{name}.yml").read_text()
            cls.jobs[name] = nested.split("\njobs:\n", 1)[1]

    def test_jobs(self):
        self.assertEqual(
            list(self.jobs),
            [
                "route",
                "agent",
                "publish",
                "verify",
                "combine",
                "review",
                "report",
                "maintain",
                "finish",
            ],
        )

    def test_jobs_that_run_agents_or_repository_code_cannot_write(self):
        for name in ("agent", "verify", "combine", "review"):
            self.assertNotIn(": write", self.jobs[name], name)
            self.assertNotIn("persist-credentials: true", self.jobs[name], name)
            self.assertNotIn("create-github-app-token", self.jobs[name], name)
            self.assertNotIn("github_app", self.jobs[name], name)
            self.assertNotIn("private-key", self.jobs[name], name)

    def permissions(self, name):
        block = re.search(r"^    permissions:\n((?:      .*\n)+)", self.jobs[name], re.M)
        return dict(re.findall(r"^      ([\w-]+): (\w+)", block.group(1), re.M))

    def test_jobs_that_run_agents_or_repository_code_hold_only_read_permissions(self):
        read = {"contents": "read", "issues": "read", "pull-requests": "read"}
        # `actions: read` downloads the result files of an earlier run's failing checks and
        # the evidence of the CI runs an issue names, `checks: read` their annotations.
        self.assertEqual(self.permissions("agent"), {"actions": "read", "checks": "read", **read})
        self.assertEqual(self.permissions("verify"), {"contents": "read"})
        self.assertEqual(self.permissions("combine"), {"contents": "read"})
        self.assertEqual(self.permissions("review"), read)

    def test_a_fix_round_downloads_the_result_files_outside_the_checkout(self):
        agent = self.jobs["agent"]
        step = agent.split("- name: Download check results\n", 1)[1].split("\n      - ", 1)[0]
        decision = "fromJSON(inputs.decision).results_run"
        self.assertIn(f"if: {decision}\n", step)
        self.assertIn("continue-on-error: true", step)  # an expired artifact never fails
        self.assertIn("name: nexkit-checks\n", step)
        self.assertIn("path: ${{ runner.temp }}/nexkit-checks\n", step)
        self.assertIn(f"run-id: ${{{{ {decision} }}}}\n", step)
        context = agent.split("- name: Collect issue context\n", 1)[1].split("\n      - ", 1)[0]
        self.assertIn('--results "$RUNNER_TEMP/nexkit-checks"', context)
        # The evidence of CI runs goes outside the checkout too, before the agent runs.
        self.assertIn('--evidence "$RUNNER_TEMP/nexkit-evidence"', context)
        self.assertLess(agent.index("Download check results"), agent.index("Run agent"))

    def test_a_head_that_conflicts_with_its_base_gets_no_checks_or_review(self):
        self.assertIn("conflicts: ${{ steps.publish.outputs.conflicts }}", self.jobs["publish"])
        self.assertIn("needs.publish.outputs.conflicts != 'true'", self.jobs["verify"])
        # The review runs only after verify succeeded, so it is skipped with it.
        self.assertIn("needs.verify.result == 'success'", self.calls["review"])
        with mock.patch("nexkit.cli._output") as output, mock.patch("builtins.print"):
            self.publish_outcome({"published": True, "pr": 6, "head": "c" * 40, "conflicts": ["a"]})
        self.assertEqual(output.call_args.kwargs["conflicts"], "true")
        with mock.patch("nexkit.cli._output") as output, mock.patch("builtins.print"):
            self.publish_outcome({"published": True, "pr": 6, "head": "c" * 40})
        self.assertEqual(output.call_args.kwargs["conflicts"], "")

    def publish_outcome(self, outcome):
        """Run `nexkit publish` with `publish` returning `outcome`."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("result.json", "context.json"):
                Path(tmp, name).write_text("{}")
            env = {"NEXKIT_DECISION": "{}", "GITHUB_REPOSITORY": "acme/app"}
            with (
                mock.patch.dict(os.environ, env),
                mock.patch("nexkit.cli._config", return_value={}),
                mock.patch("nexkit.publish.publish", return_value=outcome),
            ):
                cli.main(["publish", "--repo", tmp, "--artifacts", tmp])

    def test_verify_holds_no_secrets(self):
        for name in ("verify", "combine"):
            self.assertNotIn("secrets", self.jobs[name], name)
            self.assertNotIn("github.token", self.jobs[name], name)

    def test_parallel_checks_run_in_jobs_of_one_verify_matrix_on_the_published_commit(self):
        verify = self.jobs["verify"]
        # Every job of the matrix checks out the commit publish pushed, or the round's head.
        self.assertIn("matrix: ${{ fromJSON(needs.route.outputs.check_matrix ||", verify)
        self.assertIn("fail-fast: false", verify)
        self.assertIn("timeout-minutes: ${{ matrix.timeout }}", verify)
        self.assertIn("ref: ${{ needs.publish.outputs.head || needs.route.outputs.head }}", verify)
        self.assertIn('kit/bin/nexkit checks --repo repo --out out --group "$GROUP"', verify)
        self.assertIn("GROUP: ${{ matrix.group }}", verify)
        # Without parallel checks, the one job is `verify`, and its artifact `nexkit-checks`.
        self.assertIn(
            "name: ${{ matrix.group && format('verify ({0})', matrix.group) || 'verify' }}", verify
        )
        upload = verify.split("actions/upload-artifact@", 1)[1]
        self.assertIn(
            "matrix.group && format('nexkit-checks-{0}', matrix.group) || 'nexkit-checks'", upload
        )

    def test_no_verify_job_runs_when_route_skipped_every_check(self):
        self.assertIn("needs.route.outputs.skip_verify != 'true'", self.jobs["verify"])
        self.assertIn("skip_verify: ${{ steps.route.outputs.skip_verify }}", self.jobs["route"])
        # The review still runs, and reads the skipped checks from the decision.
        review = self.jobs["review"]
        self.assertIn("needs.route.outputs.skip_verify == 'true'", self.calls["review"])
        download = review.split("name: nexkit-checks", 1)[0].rsplit("- uses:", 1)[1]
        self.assertIn("inputs.verify_result != 'skipped'", download)
        self.assertIn("verify_result: ${{ needs.verify.result }}", self.calls["review"])
        # Combine passes the checks of the jobs that route left out.
        self.assertIn("NEXKIT_DECISION: ${{ needs.route.outputs.decision }}", self.jobs["combine"])

    def test_combine_joins_the_check_jobs_only_with_parallel_checks(self):
        combine = self.jobs["combine"]
        self.assertIn("needs: [route, verify]", combine)
        self.assertIn("needs.route.outputs.parallel_checks == 'true'", combine)
        self.assertIn("always() && needs.verify.result != 'skipped'", combine)
        self.assertIn("pattern: nexkit-checks-*\n          path: parts\n", combine)
        self.assertIn("kit/bin/nexkit combine-checks --parts parts --out out", combine)
        self.assertIn("name: nexkit-checks\n", combine.split("actions/upload-artifact@", 1)[1])
        # Nothing of the repository: NexKit's own code only.
        self.assertEqual(combine.count("actions/checkout@"), 1)
        self.assertIn("path: kit", combine)
        runs = re.findall(r"^\s+run: (.*)$", combine, re.M)
        for run in runs:
            self.assertTrue(
                run.startswith("${{ steps.python.outputs.python-path }} kit/bin/nexkit ")
            )
        # The review and report wait for it; the review only when it did not fail.
        self.assertIn("needs: [route, publish, verify, combine]", self.calls["review"])
        self.assertIn(
            'contains(fromJSON(\'["success","skipped"]\'), needs.combine.result)',
            self.calls["review"],
        )
        self.assertIn(
            "needs: [route, agent, publish, verify, combine, review]", self.jobs["report"]
        )

    def test_jobs_with_write_tokens_never_receive_model_credentials(self):
        for name in ("publish", "report", "route", "maintain", "finish"):
            job = re.sub(
                r"secrets\[(?:fromJSON\(needs\.route\.outputs\.config\)\.github_app|"
                r"steps\.app\.outputs)\.(?:app_id|private_key)_secret\]",
                "",
                self.jobs[name],
            )
            for text in (
                "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "secrets[",
                "NEXKIT_AGENT_SECRET",
            ):
                self.assertNotIn(text, job, name)
            self.assertNotIn("secrets.", job, name)

    def test_the_runner_of_an_agent_job_holds_only_its_slots(self):
        # An index known only at run time gets the job every secret, the App's key too.
        for name, call in self.calls.items():
            job = self.jobs[name]
            self.assertNotIn("secrets[", job, name)
            self.assertEqual(
                re.findall(r"\bsecrets\.(\w+)", job),
                [f"AGENT_SECRET_{n}" for n in range(1, SECRET_SLOTS + 1)],
            )
            self.assertNotIn("github_app", call, name)
            self.assertEqual(call.count("secrets["), SECRET_SLOTS, name)
            self.assertIn(f"uses: ./.github/workflows/pipeline-{name}.yml", call)
            self.assertNotIn("inherit", call, name)
        self.assertIn("needs.route.outputs.agent_secrets)[7]", self.calls["agent"])
        self.assertIn("needs.route.outputs.review_secrets)[7]", self.calls["review"])

    def test_verify_and_combine_reference_no_secret(self):
        for name in ("verify", "combine"):
            self.assertNotIn("secrets", self.jobs[name].replace("NEXKIT", ""), name)

    def test_each_job_that_writes_mints_its_token_with_only_what_it_needs(self):
        for name in ("route", "publish", "report", "maintain", "finish"):
            step = self.token_step(name)
            asked = dict(re.findall(r"permission-([a-z-]+): (read|write)", step))
            for permission, level in asked.items():
                needed = app.PERMISSIONS[permission.replace("-", "_")]
                self.assertGreaterEqual(app.LEVELS[needed], app.LEVELS[level], permission)
            # The key goes to the action, and only the token it mints to NexKit.
            uses = [s for s in self.jobs[name].split("\n      - ") if "nexkit " in s]
            for later in uses:
                self.assertNotIn("private_key_secret\n", later, name)
        self.assertEqual(self.minted("publish")["workflows"], "write")
        self.assertEqual(self.minted("finish"), {"actions": "write"})
        self.assertEqual(self.minted("route")["contents"], "read")

    def token_step(self, name):
        """The step of job `name` that mints the token the job writes with."""
        [step] = [
            s
            for s in self.jobs[name].split("\n      - ")
            if "app-token@" in s and s.startswith("id: token")
        ]
        return step

    def minted(self, name):
        return dict(re.findall(r"permission-([a-z-]+): (read|write)", self.token_step(name)))

    def test_only_a_sweep_with_the_setting_asks_for_alerts_and_it_may_fail(self):
        # A token request for a permission the App lacks fails, which must not stop a sweep.
        self.assertNotIn("vulnerability-alerts", self.token_step("maintain"))
        [step] = [
            s for s in self.jobs["maintain"].split("\n      - ") if s.startswith("id: alerts")
        ]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("decision).task == 'sweep'", step)
        self.assertIn("config).autopilot.dependabot", step)
        found = dict(re.findall(r"permission-([a-z-]+): (read|write)", step))
        self.assertEqual(found, {"vulnerability-alerts": "read"})
        self.assertEqual(app.ALERTS_PERMISSION, {"vulnerability_alerts": "read"})
        self.assertIn(
            "NEXKIT_ALERTS_TOKEN: ${{ steps.alerts.outputs.token }}", self.jobs["maintain"]
        )

    def test_nothing_writes_with_the_actions_token(self):
        for name, job in self.jobs.items():
            self.assertNotIn(
                ": write", re.sub(r"permission-[a-z-]+: write", "", job), f"{name} permissions"
            )
        for name in ("route", "publish", "report", "finish"):
            self.assertNotIn("github.token", self.jobs[name], name)
        # The wait for paused commands reads with it, since the App's token lasts an hour.
        [wait] = [
            step for step in self.jobs["maintain"].split("\n      - ") if "github.token" in step
        ]
        self.assertIn("nexkit maintain --wait", wait)
        steps = self.jobs["maintain"].split("\n      - ")
        self.assertLess(
            next(i for i, s in enumerate(steps) if "--wait" in s),
            next(i for i, s in enumerate(steps) if "app-token@" in s),
        )
        self.assertIn("steps.token.outputs.token", self.jobs["publish"].split("repository: ")[-1])

    def test_the_pipeline_declares_the_secrets_a_workflow_may_pass_one_by_one(self):
        declared = self.text.split("    secrets:\n", 1)[1].split("\npermissions:", 1)[0]
        names = re.findall(r"^      (\w+):$", declared, re.M)
        self.assertEqual(names, [*DECLARED_SECRETS, *nexkit.config.APP_SECRETS.values()])
        # A new workflow passes every secret, so agents can use secrets of any name.
        template = (ROOT / "nexkit/templates/workflow.yml").read_text()
        self.assertIn("\n    secrets: inherit\n", template)

    def test_only_the_steps_that_run_an_agent_receive_agent_secrets(self):
        steps = [(n, s) for n, job in self.jobs.items() for s in job.split("\n      - ")]
        receiving = [(n, s) for n, s in steps if "NEXKIT_AGENT_SECRET" in s]
        self.assertEqual([n for n, _ in receiving], ["agent", "review"])
        for (job, step), needed in zip(receiving, ("agent_secrets", "review_secrets"), strict=True):
            self.assertIn("kit/bin/nexkit agent ", step)
            self.assertIn("NEXKIT_AGENT_SECRETS: ${{ inputs.secret_names }}\n", step)
            slots = re.findall(
                r"NEXKIT_AGENT_SECRET_(\d+): \$\{\{ secrets\.AGENT_SECRET_(\d+) \}\}", step
            )
            numbers = [str(i + 1) for i in range(SECRET_SLOTS)]
            self.assertEqual(slots, list(zip(numbers, numbers, strict=True)), job)
            # Only the names route lists for the job's stages, each read into a slot.
            names = f"needs.route.outputs.{needed}"
            self.assertIn(f"secret_names: ${{{{ {names} }}}}\n", self.calls[job])
            read = re.findall(
                r"AGENT_SECRET_(\d+): \$\{\{ secrets\[fromJSON\((.*?)\)\[(\d+)\]\] \}\}",
                self.calls[job],
            )
            self.assertEqual(read, [(str(i + 1), names, str(i)) for i in range(SECRET_SLOTS)], job)

    def test_agent_jobs_install_the_runtimes_with_nexkit(self):
        for name in ("agent", "review"):
            self.assertIn("kit/bin/nexkit install-agents\n", self.jobs[name])
            self.assertNotIn("curl ", self.jobs[name])

    def test_jobs_with_write_tokens_run_only_nexkit(self):
        for name in ("route", "publish", "report", "maintain", "finish"):
            runs = re.findall(r"^\s+run: (.*)$", self.jobs[name], re.M)
            self.assertTrue(runs, name)
            for run in runs:
                self.assertTrue(
                    run.startswith("${{ steps.python.outputs.python-path }} kit/bin/nexkit "),
                    f"{name}: {run}",
                )

    def test_only_publish_and_report_may_write_contents(self):
        writers = [name for name, job in self.jobs.items() if "contents: write" in job]
        # publish pushes; report and maintain only merge, with nothing of the repository.
        self.assertEqual(writers, ["publish", "report", "maintain"])

    def test_report_merges_without_checking_out_the_repository(self):
        # `contents: write` lets report merge; it has nothing of the repository to run or push.
        report = self.jobs["report"]
        self.assertIn("permission-contents: write", report)
        self.assertIn("permission-actions: write", report)  # workflow_dispatch after the merge
        self.assertEqual(report.count("actions/checkout@"), 1)
        self.assertIn("path: kit", report)
        self.assertNotIn("persist-credentials: true", report)

    def test_maintain_never_checks_out_the_repository(self):
        maintain = self.jobs["maintain"]
        self.assertIn("if: needs.route.outputs.action == 'maintain'", maintain)
        self.assertIn("fromJSON(needs.route.outputs.maintain_timeout)", maintain)
        self.assertEqual(maintain.count("actions/checkout@"), 1)
        self.assertIn("path: kit", maintain)
        # `contents: write` lets a sweep merge a pull request whose required checks passed
        # since; it has nothing of the repository to run or push.
        self.assertNotIn("persist-credentials: true", maintain)
        self.assertNotIn("secrets.", maintain)
        self.assertIn('["none","maintain"]', self.jobs["report"])

    def test_finish_starts_a_sweep_only_after_a_command_whose_report_did_not_finish(self):
        finish = self.jobs["finish"]
        self.assertIn("needs: [route, report]", finish)
        # Also after a cancelled run, and only for a command that report did not finish.
        self.assertIn("always() && needs.route.result == 'success'", finish)
        self.assertIn('["plan","implement","fix","review"]', finish)
        self.assertIn("needs.report.result != 'success'", finish)
        self.assertEqual(self.permissions("finish"), {"contents": "read"})
        self.assertEqual(finish.count("actions/checkout@"), 1)
        self.assertIn("path: kit", finish)
        self.assertNotIn("persist-credentials: true", finish)

    def test_the_workflow_init_writes_gives_every_permission_a_job_asks_for(self):
        # GitHub starts no run of a workflow whose called jobs ask for more than it gives.
        given = scaffold.granted(scaffold.workflow_text("acme/kit", "v1.0.0"))
        levels = scaffold.LEVELS
        for name, job in self.jobs.items():
            block = re.search(r"^    permissions:\n((?:      .*\n)*)", job, re.M).group(1)
            for permission, level in re.findall(r"^      ([a-z-]+): (read|write)", block, re.M):
                self.assertGreaterEqual(
                    levels[given.get(permission, "none")], levels[level], f"{name}: {permission}"
                )
        self.assertIn("permission-checks: read", self.jobs["maintain"])

    def test_route_acknowledges_commands_without_repository_code(self):
        route = self.jobs["route"]
        for permission in (
            "permission-issues: write",
            "permission-pull-requests: write",
            "permission-statuses: write",
            "permission-actions: read",
        ):
            self.assertIn(permission, route)
        self.assertNotIn("contents: write", route)
        self.assertIn("sparse-checkout: .nexkit", route)

    def test_review_carries_an_approval_over_before_any_agent_or_credential_use(self):
        review = self.jobs["review"]
        steps = review.split("\n      - ")
        [carry] = [step for step in steps if "name: Carry over the approved review" in step]
        self.assertIn("if: fromJSON(inputs.decision).carry_from\n", carry)
        self.assertIn("kit/bin/nexkit carry --repo repo --out out", carry)
        self.assertNotIn("SECRET", carry)
        self.assertNotIn("GITHUB_TOKEN", carry)
        # Everything for the agent is skipped once the approval carried over.
        skipped = "if: steps.carry.outputs.carried != 'true'\n"
        for name in ("Install agents", "Collect issue context", "Run agent review"):
            [step] = [step for step in steps if f"name: {name}" in step]
            self.assertIn(skipped, step, name)
            self.assertLess(review.index("id: carry"), review.index(f"name: {name}"))
        # Its result still reaches report.
        upload = review.split("actions/upload-artifact@", 1)[1]
        self.assertIn("if: always()", upload)

    def test_review_reads_the_fix_result(self):
        review = self.jobs["review"]
        self.assertIn("name: nexkit-agent\n          path: agent\n", review)
        self.assertIn("--stage review --fix-result agent/result.json", review)

    def test_agent_step_has_no_github_token(self):
        step = self.jobs["agent"].split("- name: Run agent\n", 1)[1].split("- uses:", 1)[0]
        self.assertNotIn("GITHUB_TOKEN", step)
        review = self.jobs["review"].split("- name: Run agent review", 1)[1]
        self.assertNotIn("GITHUB_TOKEN", review.split("- uses:", 1)[0])

    def test_report_downloads_each_artifact_into_its_own_directory(self):
        # With `pattern`, a single match is extracted without its directory.
        self.assertNotIn("pattern:", self.jobs["report"])
        for name in ("nexkit-agent", "nexkit-checks", "nexkit-review"):
            self.assertIn(f"name: {name}\n          path: artifacts/{name}\n", self.jobs["report"])

    def test_report_downloads_only_artifacts_of_stages_that_ran(self):
        # A download of an artifact that cannot exist adds an error annotation to every run.
        producers = {"nexkit-agent": "agent", "nexkit-checks": "verify", "nexkit-review": "review"}
        steps = self.jobs["report"].split("\n      - ")
        downloads = [step for step in steps if "actions/download-artifact@" in step]
        self.assertEqual(len(downloads), len(producers))
        for step in downloads:
            name = re.search(r"^\s+name: (\S+)$", step, re.M).group(1)
            self.assertIn(f"\n        if: needs.{producers[name]}.result != 'skipped'\n", step)
        for name, job in producers.items():
            upload = self.jobs[job].split("actions/upload-artifact@", 1)[1]
            self.assertIn("\n        if: always()\n", upload.split("\n      - ", 1)[0], job)
            self.assertIn(f"'{name}' }}}}\n" if job == "verify" else f"name: {name}\n", upload, job)

    def test_fix_rounds_check_out_the_history_they_merge(self):
        checkout = self.jobs["agent"].split("ref: ${{ inputs.agent_ref }}", 1)[1]
        self.assertIn(
            "fetch-depth: ${{ inputs.action == 'fix' && '0' || '1' }}",
            checkout.split("- name:", 1)[0],
        )
        self.assertIn("fetch-depth: 0", self.jobs["publish"])
        self.assertIn("fetch-depth: 0", self.jobs["review"])
        # The merge base with the base branch, for NEXKIT_BASE_SHA in the checks.
        self.assertIn("fetch-depth: 0", self.jobs["verify"])
        checks = self.jobs["verify"].split("- name: Run setup and checks", 1)[1]
        self.assertIn("NEXKIT_DECISION: ${{ needs.route.outputs.decision }}", checks)

    def test_agent_and_verify_restore_caches_but_never_save(self):
        # Both jobs run code the agent wrote; a cache they saved would reach the base branch.
        uses = "uses: ./kit/.github/actions/restore-caches\n"
        setup = {"agent": "kit/bin/nexkit agent --repo", "verify": "kit/bin/nexkit checks --repo"}
        for name, job in self.jobs.items():
            if name not in setup:
                self.assertNotIn("restore-caches", job, name)
                continue
            self.assertEqual(job.count(uses), 1, name)
            self.assertLess(job.index(uses), job.index(setup[name]), name)
            step = job.split("- name: Restore caches\n", 1)[1].split("\n      - ", 1)[0]
            self.assertIn("continue-on-error: true", step, name)
            self.assertIn(".config).cache[0]", step, name)
        # A plan runs no setup.
        self.assertIn("if: inputs.action != 'plan' &&", self.jobs["agent"])

        action = (ROOT / ".github/actions/restore-caches/action.yml").read_text()
        for text in (self.text, action):
            self.assertNotRegex(text, r"actions/cache(/save)?@")
            self.assertNotIn("save-always", text)
        self.assertNotIn("secrets.", action)
        self.assertNotIn("github.token", action)
        steps = action.split("\n    - ")[1:]
        restores = [step for step in steps if "uses: actions/cache/restore@" in step]
        # One step for each entry that `cache` may have.
        self.assertEqual(len(restores), nexkit.config.MAX_CACHES)
        for number, step in enumerate(restores, 1):
            self.assertTrue(step.startswith(f"id: cache{number}\n"), step)
            self.assertIn(f"if: steps.plan.outputs.name{number}\n", step)
            self.assertIn("continue-on-error: true", step)
            self.assertIn(f"hashFiles(steps.plan.outputs.files{number})", step)
        self.assertIn("cache --report", steps[-1])

    def test_config_comes_from_default_branch(self):
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", self.jobs["route"])


if __name__ == "__main__":
    unittest.main()


class RepeatedChecksTests(unittest.TestCase):
    """Doctor warns of a workflow that runs the commands of `checks` on a person's branch,
    which `/nexkit review` checks too (#227)."""

    CHECKS = [
        {"name": "unit", "run": "python3 -m unittest discover -s tests"},
        {"name": "lint", "run": "make lint\nmake typecheck"},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".github/workflows").mkdir(parents=True)
        branch = mock.patch.object(scaffold, "default_branch_of", return_value="main")
        branch.start()
        self.addCleanup(branch.stop)

    def findings(self, triggers, steps="      - run: python3 -m unittest discover -s tests\n"):
        job = "  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        text = f"name: CI\non:\n{triggers}jobs:\n{job}{steps}"
        (self.root / ".github/workflows/ci.yml").write_text(text)
        return scaffold._repeated_checks(self.root, {"checks": self.CHECKS})

    def test_a_workflow_on_every_branch_runs_the_checks_twice(self):
        [(ok, message)] = self.findings("  push:\n  pull_request:\n")
        self.assertFalse(ok)
        self.assertIn("ci.yml runs the commands of checks `unit`", message)
        self.assertIn("through its 'push' trigger: it has no branch filter", message)
        self.assertIn("with a 'paths' filter of .github/workflows/ci.yml", message)
        self.assertIn("require `nexkit/checks` instead", message)

    def test_each_trigger_that_reaches_a_persons_branch_counts(self):
        cases = {
            "  push:\n    branches-ignore: [main]\n": "its 'branches-ignore' does not cover",
            "  push:\n    branches: ['release/*']\n": "its 'branches' include `release/",
            "  pull_request:\n    branches: [main]\n": "'pull_request' trigger: a pull request",
            "on_list": "'push' trigger",
        }
        for triggers, said in cases.items():
            with self.subTest(triggers):
                if triggers == "on_list":
                    steps = "jobs:\n  t:\n    steps:\n      - run: make lint\n"
                    text = f"on: [push, pull_request]\n{steps}"
                    (self.root / ".github/workflows/ci.yml").write_text(text)
                    found = scaffold._repeated_checks(self.root, {"checks": self.CHECKS})
                else:
                    found = self.findings(triggers)
                self.assertEqual(len(found), 1, found)
                self.assertIn(said, found[0][1])

    def test_a_workflow_on_the_default_branch_or_its_own_file_only_is_fine(self):
        for triggers in (
            "  push:\n    branches: [main]\n",
            "  push:\n    branches: [main, 'nexkit/**']\n",
            "  push:\n    branches: [main]\n"
            "  pull_request:\n    paths: ['.github/workflows/ci.yml']\n",
            "  push:\n    paths: [.github/workflows/ci.yml]\n",
            "  pull_request:\n    branches: [develop]\n",
            "  schedule:\n    - cron: '0 0 * * *'\n",
        ):
            with self.subTest(triggers):
                self.assertEqual(self.findings(triggers), [])

    def test_only_a_command_of_checks_counts_as_a_whole(self):
        triggers = "  push:\n"
        self.assertEqual(self.findings(triggers, "      - run: make lint-docs\n"), [])
        block = "      - name: Test\n        run: |\n          cd app\n          make lint\n"
        [(_, message)] = self.findings(triggers, block)
        self.assertIn("`lint` (`make lint`)", message)
        quoted = '      - run: "cd app && python3 -m unittest discover -s tests"\n'
        self.assertEqual(len(self.findings(triggers, quoted)), 1)

    def test_a_code_scan_that_builds_the_code_is_left_out(self):
        steps = (
            "      - uses: github/codeql-action/init@v3\n"
            "      - run: python3 -m unittest discover -s tests\n"
        )
        self.assertEqual(self.findings("  pull_request:\n", steps), [])

    def test_nexkits_own_workflow_and_a_repository_without_checks_are_left_out(self):
        (self.root / ".github/workflows/nexkit.yml").write_text(
            "on: [push]\njobs:\n  t:\n    steps:\n      - run: make lint\n"
        )
        self.assertEqual(scaffold._repeated_checks(self.root, {"checks": self.CHECKS}), [])
        self.findings("  push:\n")
        self.assertEqual(scaffold._repeated_checks(self.root, {"checks": []}), [])

    def test_doctor_reports_it(self):
        with mock.patch.object(scaffold, "github_app", return_value=(APP, [])):
            cli.main(["init", "--repo", str(self.root), "--check", "test=make test", "--force"])
        (self.root / ".github/workflows/ci.yml").write_text(
            "on: [push]\njobs:\n  t:\n    steps:\n      - run: make test\n"
        )
        failed = [m for ok, m in scaffold.doctor(self.root) if not ok]
        self.assertTrue(any("ci.yml runs the commands of checks `test`" in m for m in failed))
