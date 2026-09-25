"""Configured reasoning reaches the official CLI action without model calls."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.ci import prepare_job
from nexkit.clarify import main as clarify_main
from nexkit.common import Blocked
from nexkit.policy import candidate_key, config
from tests.support import FakeGitHub, issue, project
from tests.test_clarify import RequirementGitHub


class ReasoningTests(unittest.TestCase):
    def test_optional_effort_preserves_existing_configuration(self):
        cfg = project()
        self.assertNotIn("reasoning_effort", config(cfg))
        cfg["reasoning_effort"] = {"implement": "max", "review": "high"}
        self.assertEqual(config(cfg)["reasoning_effort"], cfg["reasoning_effort"])

    def test_invalid_or_partial_effort_is_rejected(self):
        for value in (
            None,
            "max",
            {},
            {"implement": "max"},
            {"implement": "max", "review": "max", "request": "max"},
            {"implement": "maximum", "review": "max"},
            {"implement": "max\nmodel=other", "review": "max"},
            {"implement": [], "review": "max"},
        ):
            with self.subTest(value=value), self.assertRaises(Blocked):
                config({**project(), "reasoning_effort": value})

    def test_effort_change_invalidates_candidate_identity(self):
        cfg = project()
        original = candidate_key(issue(), cfg, "b" * 40, "c" * 40)
        changed = deepcopy(cfg)
        changed["reasoning_effort"] = {"implement": "max", "review": "max"}
        self.assertNotEqual(original, candidate_key(issue(), changed, "b" * 40, "c" * 40))

    def prepare_environment(self, path, gh):
        event = {"issue": gh.work, "sender": {"login": "owner", "type": "User"}}
        (path / "event.json").write_text(json.dumps(event))
        return {
            "GITHUB_REPOSITORY": gh.repository,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_EVENT_PATH": str(path / "event.json"),
            "GITHUB_OUTPUT": str(path / "output"),
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "1",
        }

    def test_delivery_exports_each_role_effort_and_omitted_defaults(self):
        for efforts in (None, {"implement": "max", "review": "high"}):
            with self.subTest(efforts=efforts), tempfile.TemporaryDirectory() as directory:
                gh, path = FakeGitHub(), Path(directory)
                if efforts is not None:
                    gh.cfg["reasoning_effort"] = efforts
                with (
                    patch.dict(os.environ, self.prepare_environment(path, gh)),
                    patch("nexkit.ci.GitHub", return_value=gh),
                ):
                    context = prepare_job(path / "context.json", "a" * 40)
                self.assertTrue(context["ready"])
                outputs = dict(
                    line.split("=", 1) for line in (path / "output").read_text().splitlines()
                )
                for role in ("implement", "review"):
                    self.assertEqual(outputs[f"{role}_effort"], (efforts or {}).get(role, ""))

    def test_clarification_exports_implementation_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            gh, path = RequirementGitHub(), Path(directory)
            gh.cfg["reasoning_effort"] = {"implement": "max", "review": "high"}
            argv = [
                "clarify",
                "prepare",
                "--context",
                str(path / "context.json"),
                "--kit-ref",
                "a" * 40,
            ]
            with (
                patch.dict(os.environ, self.prepare_environment(path, gh)),
                patch("nexkit.github.GitHub", return_value=gh),
                patch("sys.argv", argv),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(clarify_main(), 0)
            self.assertIn("effort=max\n", (path / "output").read_text())

    def test_every_agent_action_receives_the_corresponding_output(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        cases = {"delivery.yml": ("implement_effort", "review_effort"), "clarify.yml": ("effort",)}
        for name, outputs in cases.items():
            text = (workflows / name).read_text()
            for output in outputs:
                with self.subTest(workflow=name, output=output):
                    self.assertIn(f"{output}: ${{{{ steps.prepare.outputs.{output} }}}}", text)
                    self.assertIn(f"effort: ${{{{ needs.prepare.outputs.{output} }}}}", text)


if __name__ == "__main__":
    unittest.main()
