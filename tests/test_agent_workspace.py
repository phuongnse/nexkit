"""Shared restoration contracts and explicitly enabled native setup acceptance."""

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from nexkit.agent_workspace import prepare
from nexkit.ci import materialize
from nexkit.common import Blocked, read_json
from nexkit.workspace import directory_identity, restore, snapshot
from tests.support import project


def fixture(root):
    source, home, data = (root / name for name in ("source", "home", "data"))
    source.mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    (source / "app.py").write_text("print('fixture')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "app.py"], check=True)
    cfg = project()
    cfg["environment"]["agent_runner"] = "ubuntu-24.04"
    context = {"config": cfg, "source": "b" * 40, "base": "b" * 40}
    return source, home, data, context


class WorkspaceRestorationTests(unittest.TestCase):
    def test_managed_agent_jobs_prepare_runtime_and_use_the_shared_private_directory(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / ".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            for name, job in workflow["jobs"].items():
                steps = job.get("steps", [])
                for index, step in enumerate(steps):
                    if step.get("uses") != "./kit/actions/agent-workspace":
                        continue
                    with self.subTest(workflow=path.name, job=name):
                        self.assertTrue(
                            any(
                                s.get("uses") == "./kit/actions/python-runtime"
                                for s in steps[:index]
                            )
                        )
                        self.assertTrue(step["with"]["context"].startswith("/tmp/nexkit/"))
                        self.assertNotIn("${{ runner.temp }}/nexkit", str(steps))

    def test_agent_jobs_guard_before_setup_and_again_before_the_cli(self):
        root = Path(__file__).resolve().parents[1]
        for name, job in (
            ("delivery", "implement"),
            ("delivery", "review"),
            ("clarify", "clarify"),
            ("agent-invocation", "execute"),
        ):
            with self.subTest(workflow=name, job=job):
                workflow = yaml.safe_load((root / f".github/workflows/{name}.yml").read_text())
                steps = workflow["jobs"][job]["steps"]
                workspace = next(
                    i
                    for i, step in enumerate(steps)
                    if step.get("uses") == "./kit/actions/agent-workspace"
                )
                guards = [
                    i
                    for i, step in enumerate(steps)
                    if any(
                        command in step.get("run", "")
                        for command in ("'guard'", "'guard-invocation'")
                    )
                ]
                agent = next(
                    i
                    for i, step in enumerate(steps)
                    if step.get("uses") == "./kit/actions/agent-session"
                )
                self.assertEqual(len(guards), 2)
                self.assertLess(guards[0], workspace)
                self.assertLess(workspace, guards[1])
                self.assertLess(guards[1], agent)
                for index in guards:
                    self.assertEqual(steps[index]["env"]["GH_TOKEN"], "${{ github.token }}")
                self.assertNotIn("GH_TOKEN", steps[workspace].get("env", {}))

    def test_native_restore_preserves_dependencies_and_replaces_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, home, data, context = fixture(Path(temporary))
            work = home / "work"
            materialize(source, work, context, "task", data)
            skill = work / ".agents/skills/nexkit-task/SKILL.md"
            original = skill.read_text(encoding="utf-8")
            snapshot(home, data)
            skill.write_text("Untrusted replacement", encoding="utf-8")
            (work / ".git/config").write_text("invalid configuration", encoding="utf-8")
            paths = [
                home / ".gitconfig",
                home / ".config/git/config",
                home / "Documents/PowerShell/profile.ps1",
                home / "Documents/WindowsPowerShell/profile.ps1",
                home / "AppData/Local/Git/config",
                home / "AppData/Roaming/Git/config",
                home / ".codex/config.toml",
                work / ".codex/config.toml",
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Untrusted startup control", encoding="utf-8")
            dependency = home / ".local/dependency.txt"
            dependency.parent.mkdir()
            dependency.write_text("Installed dependency", encoding="utf-8")
            ids = {} if os.name == "nt" else {"uid": os.getuid(), "gid": os.getgid()}
            restore(home, data, role="task", **ids)
            self.assertEqual(skill.read_text(encoding="utf-8"), original)
            self.assertEqual(dependency.read_text(encoding="utf-8"), "Installed dependency")
            for path in paths:
                if path == home / ".codex/config.toml":
                    self.assertIn('approval_policy = "never"', path.read_text(encoding="utf-8"))
                else:
                    self.assertFalse(path.exists(), path)
            for key, expected in (("core.hooksPath", os.devnull), ("core.fsmonitor", "false")):
                result = subprocess.run(
                    ["git", "-C", str(work), "config", key],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), expected)

    def test_startup_parent_link_does_not_change_an_outside_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home, data, context = fixture(root)
            materialize(source, home / "work", context, "task", data)
            snapshot(home, data)
            outside = root / "outside"
            (outside / "PowerShell").mkdir(parents=True)
            canary = outside / "PowerShell/profile.ps1"
            canary.write_text("Must remain unchanged", encoding="utf-8")
            (home / "Documents").symlink_to(outside, target_is_directory=True)
            ids = {} if os.name == "nt" else {"uid": os.getuid(), "gid": os.getgid()}
            restore(home, data, role="task", **ids)
            self.assertEqual(canary.read_text(encoding="utf-8"), "Must remain unchanged")
            self.assertFalse((home / "Documents").exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows directory handles")
    def test_pinned_directories_cannot_be_renamed_and_reparse_roots_are_rejected(self):
        from nexkit.windows_filesystem import pinned_directories

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, work = root / "home", root / "home/work"
            work.mkdir(parents=True)
            identities = {path: directory_identity(path) for path in (home, work)}
            with pinned_directories(home, work):
                for path in (home, work):
                    with self.assertRaises(OSError):
                        path.rename(path.with_name("replaced"))
                    self.assertEqual(directory_identity(path), identities[path])
            # Closing the handles must release the restriction.
            replacement = work.with_name("replaced")
            work.rename(replacement)
            replacement.rename(work)
            link = root / "linked-home"
            link.symlink_to(home, target_is_directory=True)
            with self.assertRaises(Blocked), pinned_directories(link / "work"):
                self.fail("A reparse parent was accepted")
            with self.assertRaises(Blocked):
                directory_identity(link)


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("NEXKIT_TEST_AGENT_WORKSPACE") == "1",
    "Native agent workspace acceptance requires a fresh disposable runner",
)
class LinuxAgentWorkspaceTests(unittest.TestCase):
    def test_real_setup_keeps_dependencies_and_restores_trusted_controls(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as cleanup:
            control = Path(temporary)
            source, _, data, context = fixture(control)
            # Production snapshots remain administrator-owned.
            cleanup.callback(
                subprocess.run, ["sudo", "-n", "rm", "-rf", "--", str(data)], check=True
            )
            script = """import os, pathlib, subprocess
home, work = pathlib.Path.home(), pathlib.Path.cwd()
assert not any(name in os.environ for name in ('GH_TOKEN', 'OPENAI_API_KEY', 'PYTHONPATH'))
import pwd
assert pwd.getpwuid(os.geteuid()).pw_name == 'nexkit-agent'
dependency = home / '.local/dependency.txt'
dependency.parent.mkdir(parents=True, exist_ok=True)
dependency.write_text('Native setup completed', encoding='utf-8')
for path in (home / '.gitconfig', home / '.codex/config.toml',
             work / '.codex/config.toml', home / 'Documents/PowerShell/profile.ps1'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Untrusted startup control', encoding='utf-8')
print('Isolated native dependency setup completed')
"""
            context["config"]["environment"]["setup"] = [[sys.executable, "-I", "-c", script]]
            with patch.dict(
                os.environ,
                {"GH_TOKEN": "harmless-github-canary", "OPENAI_API_KEY": "harmless-model-canary"},
            ):
                result = prepare(source, data, context, "task")
            paths = result["paths"]
            home, work = Path(paths["NEXKIT_AGENT_HOME"]), Path(paths["NEXKIT_WORKSPACE"])
            self.assertEqual(len(result["setup"]), 1)
            self.assertEqual(result["setup"][0]["exit_code"], 0, result)
            self.assertEqual(
                (home / ".local/dependency.txt").read_text(encoding="utf-8"),
                "Native setup completed",
            )
            self.assertFalse((home / ".gitconfig").exists())
            self.assertFalse((work / ".codex").exists())
            self.assertFalse((home / "Documents/PowerShell").exists())
            self.assertTrue((work / ".agents/skills/nexkit-task/SKILL.md").is_file())
            self.assertEqual((work / "app.py").read_text(), "print('fixture')\n")
            self.assertTrue((home / "output").is_dir())
            self.assertEqual(read_json(data / "schema.json")["type"], "object")
            self.assertIn('approval_policy = "never"', (home / ".codex/config.toml").read_text())


if __name__ == "__main__":
    unittest.main()
