"""Independent native check jobs run commands locally; GitHub state is mocked."""

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

from nexkit.checks import combine_checks, verify
from nexkit.ci import prepare_check
from nexkit.common import Blocked
from nexkit.delivery import finish, prepare, publish
from tests.support import FakeGitHub, reviewed, verified
from tests.test_delivery import bundle


class CheckJobTests(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.cfg["checks"].append(
            {
                "name": "lint",
                "kind": "lint",
                "argv": ["python3", "-c", "pass"],
                "timeout_seconds": 10,
            }
        )
        context = prepare(self.gh, 1, "100.1", "a" * 40)
        self.context = publish(self.gh, context, bundle(context))

    def reports(self):
        reports = []
        for check in self.gh.cfg["checks"]:
            reports.append(
                {
                    "candidate": self.context["candidate"],
                    "passed": True,
                    "producer": {"check": check["name"], "run_key": self.context["run_key"]},
                    "checks": [
                        {"name": check["name"], "kind": check["kind"], "passed": True, "tests": 1}
                    ],
                }
            )
        return reports

    def test_unit_and_e2e_success_cannot_hide_a_missing_required_lint_job(self):
        key = self.context["candidate"]
        outcome = finish(self.gh, self.context, verified(key), reviewed(key))
        self.assertNotEqual(outcome["status"], "merged")
        self.assertIn("Missing, duplicate", outcome["reason"])
        self.assertEqual(self.gh.merges, [])

    def test_each_native_job_must_produce_its_own_current_configured_check(self):
        for mutation in (
            "missing",
            "duplicate",
            "wrong-name",
            "wrong-kind",
            "old-run",
            "old-candidate",
            "wrong-producer",
        ):
            reports = deepcopy(self.reports())
            if mutation == "missing":
                reports.pop()
            elif mutation == "duplicate":
                reports.append(deepcopy(reports[0]))
            elif mutation == "wrong-name":
                reports[0]["checks"][0]["name"] = "unknown"
                reports[0]["producer"]["check"] = "unknown"
            elif mutation == "wrong-kind":
                reports[0]["checks"][0]["kind"] = "lint"
            elif mutation == "old-run":
                reports[0]["producer"]["run_key"] = "99.1"
            elif mutation == "old-candidate":
                reports[0]["candidate"]["head"] = "f" * 40
            else:
                reports[0]["producer"]["check"] = "another-job"
            with self.subTest(mutation=mutation), self.assertRaises(Blocked):
                combine_checks(self.context, reports)

    def test_failed_command_remains_feedback_and_blocks_merge(self):
        reports = self.reports()
        reports[0]["passed"] = False
        reports[0]["checks"][0].update(passed=False, log="Actual assertion failed")
        result = combine_checks(self.context, reports)
        self.assertFalse(result["passed"])
        outcome = finish(self.gh, self.context, result, reviewed(self.context["candidate"]))
        self.assertEqual(self.gh.merges, [])
        self.assertIn("Actual assertion failed", str(outcome["feedback"]))

    def test_complete_native_job_results_allow_normal_merge(self):
        result = combine_checks(self.context, self.reports())
        outcome = finish(self.gh, self.context, result, reviewed(self.context["candidate"]))
        self.assertEqual(outcome["status"], "merged")

    def test_check_guard_rejects_other_runs_unknown_commands_and_unpublished_candidates(self):
        env = {
            "GITHUB_REPOSITORY": self.gh.repository,
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        with patch.dict(os.environ, env), patch("nexkit.ci.GitHub", return_value=self.gh):
            self.assertTrue(prepare_check(self.context, "lint", "a" * 40)["authorized"])
            with self.assertRaisesRegex(Blocked, "Unknown"):
                prepare_check(self.context, "missing", "a" * 40)
            changed = deepcopy(self.context)
            changed["candidate"]["head"] = "f" * 40
            with self.assertRaisesRegex(Blocked, "published"):
                prepare_check(changed, "lint", "a" * 40)
            with (
                patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}),
                self.assertRaisesRegex(Blocked, "run attempt"),
            ):
                prepare_check(self.context, "lint", "a" * 40)

    def test_single_check_executes_actual_cases_without_running_its_siblings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_real.py").write_text(
                "import unittest\nclass Actual(unittest.TestCase):\n    def test_sum(self):\n        self.assertEqual(-2 + 5, 3)\n"
            )
            cfg = deepcopy(self.gh.cfg)
            cfg["checks"][0]["argv"] = ["python3", "-m", "unittest", "test_real"]
            cfg["checks"][1]["argv"] = ["python3", "-c", "raise SystemExit(9)"]
            single = verify(cfg, root, self.context["candidate"], check_names=["test"])
            self.assertTrue(single["passed"], single)
            self.assertEqual(single["checks"][0]["tests"], 1)
            self.assertEqual(len(single["checks"]), 1)
            self.assertFalse(verify(cfg, root, self.context["candidate"])["passed"])
            with self.assertRaises(Blocked):
                verify(cfg, root, check_names=["does-not-exist"])

    def test_environment_failure_is_preserved_without_claiming_a_check_executed(self):
        cfg = deepcopy(self.gh.cfg)
        cfg["environment"]["setup"] = [
            ["python3", "-c", "print('SETUP_FIXTURE_FAILED'); raise SystemExit(3)"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            failed = verify(cfg, directory, self.context["candidate"], check_names=["test"])
        failed["producer"] = {"check": "test", "run_key": self.context["run_key"]}
        self.assertFalse(failed["checks"][0]["executed"])
        self.assertNotIn("tests", failed["checks"][0])
        reports = self.reports()
        reports[0] = failed
        combined = combine_checks(self.context, reports)
        self.assertFalse(combined["passed"])
        self.assertIn("SETUP_FIXTURE_FAILED", str(combined["checks"][0]))
        outcome = finish(self.gh, self.context, combined, reviewed(self.context["candidate"]))
        self.assertEqual(self.gh.merges, [])
        self.assertIn("SETUP_FIXTURE_FAILED", str(outcome["feedback"]))


class WorkflowStructureTests(unittest.TestCase):
    def test_queue_field_not_yet_known_to_actionlint_keeps_native_fifo_contract(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        checked = []
        for path in workflows.glob("*.yml"):
            workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            for owner in [workflow, *workflow.get("jobs", {}).values()]:
                concurrency = owner.get("concurrency", {})
                if isinstance(concurrency, dict) and "queue" in concurrency:
                    self.assertEqual(concurrency["queue"], "max", path.name)
                    self.assertTrue(concurrency.get("group"), path.name)
                    self.assertIn(concurrency.get("cancel-in-progress"), (None, "false"), path.name)
                    checked.append(path.name)
        self.assertTrue(
            {"intake.yml", "delivery.yml", "clarify.yml", "release.yml"} <= set(checked)
        )

    def test_check_capability_has_no_write_permission_or_provider_secret(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/candidate-check.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn("secrets", workflow["on"]["workflow_call"])
        job = workflow["jobs"]["check"]
        self.assertEqual(set(job["permissions"].values()), {"read"})
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
