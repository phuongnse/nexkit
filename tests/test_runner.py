"""Consumer routing, admission and native permission configuration; no model calls."""

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.ci import prepare_job
from nexkit.common import Blocked
from nexkit.policy import agent_runner, authentication, candidate_key, config
from nexkit.project import caller
from nexkit.subscription import cli_command, execute, permission_settings, toml
from nexkit.workspace import restore, snapshot, unchanged
from runner.job_hook import admitted
from tests import test_reasoning
from tests.support import FakeGitHub, issue, project


def subscription_project():
    cfg = project()
    cfg["engine"]["auth"] = "chatgpt"
    cfg["environment"]["agent_runner"] = ["self-hosted", "linux", "x64", "consumer-one"]
    return cfg


class RunnerTests(unittest.TestCase):
    def workspace_fixture(self, root):
        home, data = root / "home", root / "data"
        work = home / "work"
        work.mkdir(parents=True)
        data.mkdir()
        subprocess.run(["git", "init", "--quiet", str(work)], check=True)
        snapshot(home, data)
        return home, data

    def test_setup_restore_preserves_dependencies_and_repairs_git_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            home, data = self.workspace_fixture(Path(directory))
            dependency = home / ".local/lib/installed.txt"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("installed dependency")
            executable = home / "work/.venv/bin/tool"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture")
            executable.chmod(0o755)
            (home / ".bashrc").write_text("untrusted startup data")
            (home / ".gitconfig").write_text("untrusted Git configuration")
            (home / "work/.git/config").write_text("invalid setup configuration")
            restore(home, data, uid=os.getuid(), gid=os.getgid(), role="deliver")
            self.assertEqual(dependency.read_text(), "installed dependency")
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            self.assertFalse((home / ".bashrc").exists())
            self.assertFalse((home / ".gitconfig").exists())
            for path in (home / "work/.git", *(home / "work/.git").rglob("*")):
                self.assertEqual(path.lstat().st_uid, os.getuid())
            result = subprocess.run(
                ["git", "-C", str(home / "work"), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn(".agents/", result.stdout)
            git_config = (home / "work/.git/config").read_text()
            self.assertIn("hooksPath = /dev/null", git_config)
            self.assertIn("fsmonitor = false", git_config)

    def test_session_lock_precedes_every_account_operation(self):
        events = []

        @contextmanager
        def lock_file(*args):
            yield 19

        with (
            patch("nexkit.subscription.binding"),
            patch("nexkit.subscription.unchanged"),
            patch("nexkit.subscription.open", side_effect=lock_file),
            patch("nexkit.subscription.fcntl.flock", side_effect=lambda *a: events.append("lock")),
            patch("nexkit.subscription.run_session", side_effect=lambda *a: events.append("run")),
        ):
            execute({"config": subscription_project()}, "deliver")
        self.assertEqual(events, ["lock", "run"])
        with (
            patch("nexkit.subscription.binding"),
            patch("nexkit.subscription.unchanged"),
            patch("nexkit.subscription.open", side_effect=lock_file),
            patch("nexkit.subscription.fcntl.flock", side_effect=BlockingIOError),
            patch("nexkit.subscription.run_session") as run,
            self.assertRaises(BlockingIOError),
        ):
            execute({"config": subscription_project()}, "deliver")
        run.assert_not_called()

    def test_parent_metadata_git_is_disabled_without_changing_tool_git(self):
        shim = Path(__file__).resolve().parents[1] / "runner/parent-git.sh"
        result = subprocess.run(["/bin/sh", str(shim), "status"], capture_output=True)
        self.assertEqual(result.returncode, 127)
        settings = permission_settings("/auth", "/work", "/scratch", writable=True)
        self.assertEqual(
            settings["shell_environment_policy.set"]["PATH"], "/usr/local/bin:/usr/bin:/bin"
        )

    def test_setup_cannot_replace_workspace_with_directory_or_symlink(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home, data = self.workspace_fixture(root)
                outside = root / "outside"
                outside.mkdir()
                (outside / "canary").write_text("unchanged")
                (home / "work").rename(home / "original")
                if symlink:
                    (home / "work").symlink_to(outside, target_is_directory=True)
                else:
                    (home / "work").mkdir()
                with self.assertRaises(Blocked):
                    restore(home, data, uid=os.getuid(), gid=os.getgid(), role="deliver")
                self.assertEqual(list(outside.iterdir()), [outside / "canary"])
                self.assertEqual((outside / "canary").read_text(), "unchanged")

    def test_setup_cannot_replace_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, data = self.workspace_fixture(root)
            home.rename(root / "original")
            shutil.copytree(root / "original", home)
            with self.assertRaises(Blocked):
                unchanged(home, data)

    def test_existing_hosted_api_configuration_is_unchanged(self):
        cfg = config(project())
        self.assertEqual(authentication(cfg), "api-key")
        self.assertEqual(agent_runner(cfg), "ubuntu-24.04")

    def test_each_consumer_selects_its_own_runner(self):
        one, two = subscription_project(), subscription_project()
        two["environment"]["agent_runner"][-1] = "consumer-two"
        self.assertNotEqual(agent_runner(config(one)), agent_runner(config(two)))
        for cfg in (one, two):
            self.assertNotIn("OPENAI_API_KEY", caller(cfg, "delivery"))
        self.assertIn("OPENAI_API_KEY", caller(project(), "delivery"))
        self.assertNotIn("OPENAI_API_KEY", caller(project(), "release"))

    def test_invalid_or_ambiguous_runner_is_rejected(self):
        for runner in (
            None,
            "self-hosted",
            [],
            ["self-hosted", "linux", "x64"],
            ["self-hosted", "linux", "x64", "bad\nlabel"],
            ["self-hosted", "linux", "x64", []],
            ["self-hosted", "linux", "x64", "self-hosted"],
        ):
            with self.subTest(runner=runner), self.assertRaises(Blocked):
                cfg = subscription_project()
                cfg["environment"]["agent_runner"] = runner
                config(cfg)

    def test_chatgpt_cannot_silently_use_hosted_runner_or_unverified_cli(self):
        cfg = subscription_project()
        del cfg["environment"]["agent_runner"]
        with self.assertRaises(Blocked):
            config(cfg)
        cfg = subscription_project()
        cfg["engine"]["version"] = "0.155.0"
        with self.assertRaises(Blocked):
            config(cfg)

    def test_runner_or_auth_changes_invalidate_previous_candidate(self):
        cfg = project()
        old = candidate_key(issue(), cfg, "b" * 40, "c" * 40)
        for changed in (subscription_project(), deepcopy(cfg)):
            changed["environment"]["agent_runner"] = ["self-hosted", "linux", "x64", "own-runner"]
            self.assertNotEqual(old, candidate_key(issue(), changed, "b" * 40, "c" * 40))

    def test_authorized_controller_exports_the_consumer_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            gh, root = FakeGitHub(), Path(directory)
            gh.cfg = subscription_project()
            env = test_reasoning.ReasoningTests().prepare_environment(root, gh)
            with patch.dict(os.environ, env), patch("nexkit.ci.GitHub", return_value=gh):
                self.assertTrue(prepare_job(root / "context.json", "a" * 40)["ready"])
            output = dict(line.split("=", 1) for line in (root / "output").read_text().splitlines())
            self.assertEqual(output["authentication"], "chatgpt")
            self.assertEqual(json.loads(output["agent_runner"]), agent_runner(gh.cfg))

    def test_profile_denies_auth_and_keeps_external_capabilities_disabled(self):
        settings = permission_settings("/auth", "/work", "/scratch", writable=True)
        parsed = tomllib.loads("settings=" + toml(settings))["settings"]
        self.assertEqual(parsed, settings)
        fs = parsed["permissions.nexkit.filesystem"]
        self.assertEqual(fs["/auth"], "deny")
        self.assertEqual(fs["/work"], "write")
        self.assertEqual(fs["/work/.agents"], "read")
        self.assertFalse(parsed["permissions.nexkit.network.enabled"])
        self.assertFalse(parsed["features.hooks"])
        self.assertFalse(parsed["features.plugins"])
        self.assertEqual(parsed["mcp_servers"], {})
        command = cli_command(subscription_project(), "review", "/scratch")
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn('forced_login_method="chatgpt"', command)

    def test_admission_requires_bound_repo_branch_trigger_and_workflow(self):
        bound = {
            "repository": "owner/one",
            "default_branch": "main",
            "workflows": ["nexkit-delivery.yml"],
        }
        env = {
            "GITHUB_REPOSITORY": "owner/one",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_REF": "owner/one/.github/workflows/nexkit-delivery.yml@refs/heads/main",
        }
        self.assertTrue(admitted(bound, env))
        for key, bad in (
            ("GITHUB_REPOSITORY", "owner/two"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_EVENT_NAME", "pull_request_target"),
            ("GITHUB_WORKFLOW_REF", "owner/one/.github/workflows/evil.yml@refs/heads/main"),
        ):
            with self.subTest(key=key, value=bad):
                self.assertFalse(admitted(bound, {**env, key: bad}))
        self.assertFalse(admitted(bound, {}))


if __name__ == "__main__":
    unittest.main()
