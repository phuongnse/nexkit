import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nexkit
from nexkit import cli, scaffold
from tests.support import FakeGitHub, git

ROOT = Path(__file__).resolve().parent.parent


class InitDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

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
        self.assertIn("uses: phuongnse/nexkit/.github/workflows/pipeline.yml@v1.0.0", workflow)
        self.assertIn("nexkit_ref: v1.0.0", workflow)
        self.assertNotIn("__KIT", workflow)
        self.assertEqual(cli.main(["init", "--repo", str(self.root)]), 2)  # refuses overwrite
        self.assertEqual(cli.main(["init", "--repo", str(self.root), "--force"]), 0)

    def test_init_rejects_bad_check(self):
        self.assertEqual(cli.main(["init", "--repo", str(self.root), "--check", "npm test"]), 2)
        self.assertFalse((self.root / ".nexkit").exists())

    def test_doctor_without_github(self):
        cli.main(["init", "--repo", str(self.root), "--check", "test=true"])
        git(self.root, "init", "-q")
        findings = scaffold.doctor(self.root)
        self.assertIn((True, ".nexkit/config.json is valid"), findings)
        self.assertTrue(any("pipeline at v1.0.0" in m for ok, m in findings if ok))
        self.assertIn((False, "The 'origin' remote is not a GitHub repository"), findings)
        self.assertEqual(cli.main(["doctor", "--repo", str(self.root)]), 1)

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
        cli.main(["init", "--repo", str(self.root / "repo"), "--check", "test=true"])
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
        self.assertEqual(out["claude_version"], nexkit.CLAUDE_CODE)
        self.assertEqual(json.loads(out["decision"])["issue"], 5)
        self.assertEqual(json.loads(out["config"])["checks"][0]["name"], "test")
        self.assertEqual(out["agent_ref"], "main")
        self.assertEqual(out["agent_timeout"], "75")
        self.assertEqual(self.gh.reactions, [(3, "eyes")])

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
            # The Claude Code version is read from nexkit/__init__.py, never repeated.
            self.assertNotIn(nexkit.CLAUDE_CODE, text, path.name)

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
        text = (ROOT / ".github/workflows/pipeline.yml").read_text()
        body = text.split("\njobs:\n", 1)[1]
        cls.jobs = dict(re.findall(r"^  (\w+):\n((?:    .*\n|\n)*)", body, re.M))

    def test_jobs(self):
        self.assertEqual(
            list(self.jobs), ["route", "agent", "publish", "verify", "review", "report"]
        )

    def test_jobs_that_run_agents_or_repository_code_cannot_write(self):
        for name in ("agent", "verify", "review"):
            self.assertNotIn(": write", self.jobs[name], name)
            self.assertNotIn("persist-credentials: true", self.jobs[name], name)
            self.assertNotIn("NEXKIT_PUSH_TOKEN", self.jobs[name], name)

    def test_verify_holds_no_secrets(self):
        self.assertNotIn("secrets.", self.jobs["verify"])
        self.assertNotIn("github.token", self.jobs["verify"])

    def test_jobs_with_write_tokens_never_receive_model_credentials(self):
        for name in ("publish", "report", "route"):
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.jobs[name], name)
            self.assertNotIn("ANTHROPIC_API_KEY", self.jobs[name], name)

    def test_agent_step_has_no_github_token(self):
        step = self.jobs["agent"].split("- name: Run Claude Code", 1)[1].split("- uses:", 1)[0]
        self.assertNotIn("GITHUB_TOKEN", step)
        review = self.jobs["review"].split("- name: Review with Claude Code", 1)[1]
        self.assertNotIn("GITHUB_TOKEN", review.split("- uses:", 1)[0])

    def test_report_downloads_each_artifact_into_its_own_directory(self):
        # With `pattern`, a single match is extracted without its directory.
        self.assertNotIn("pattern:", self.jobs["report"])
        for name in ("nexkit-agent", "nexkit-checks", "nexkit-review"):
            self.assertIn(f"name: {name}\n          path: artifacts/{name}\n", self.jobs["report"])

    def test_config_comes_from_default_branch(self):
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", self.jobs["route"])


if __name__ == "__main__":
    unittest.main()
