"""Actual setup-python and workspace actions in the unmodified runner image.

No GitHub registration, credentials or model calls are supplied by this test.
"""

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

import yaml

from tests.test_agent_workspace import fixture

ROOT = Path(__file__).resolve().parents[1]


def action_environment(path):
    values = {}
    lines = iter(path.read_text().splitlines())
    for line in lines:
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            value = []
            for item in lines:
                if item == delimiter:
                    break
                value.append(item)
            else:
                raise ValueError("Incomplete Actions environment record")
            values[key] = "\n".join(value)
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("NEXKIT_TEST_ACTIONS_RUNTIME") == "1",
    "Real setup-python in a fresh disposable NexKit image",
)
class ActionsRuntimeTests(unittest.TestCase):
    def test_workflow_python_survives_sudo_and_runs_snapshot_and_restore(self):
        self.assertTrue(Path("/.dockerenv").is_file())
        self.assertEqual(os.geteuid(), 0)
        # Pre-login Actions probes use the Python shell before setup-python.
        subprocess.run(
            ["python", "-I", "-c", "import sys; assert sys.version_info >= (3, 11)"], check=True
        )
        runtime = yaml.safe_load((ROOT / "actions/python-runtime/action.yml").read_text())
        setup = runtime["runs"]["steps"][0]
        repository, commit = setup["uses"].split("@")
        self.assertEqual(repository, "actions/setup-python")
        self.assertEqual(len(commit), 40)
        temp = Path("/opt/actions-runner/_work/_temp")
        temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp) as temporary:
            root = Path(temporary)
            archive = root / "setup-python.tar.gz"
            with urlopen(
                f"https://api.github.com/repos/{repository}/tarball/{commit}", timeout=60
            ) as response:
                archive.write_bytes(response.read())
            with tarfile.open(archive) as source:
                source.extractall(root / "action", filter="data")
            action = next((root / "action").iterdir())
            definition = yaml.safe_load((action / "action.yml").read_text())
            defaults = {
                "INPUT_" + key.upper(): str(value.get("default", ""))
                for key, value in definition["inputs"].items()
                if not str(value.get("default", "")).startswith("${{")
            }
            env_file, path_file, output_file = (root / name for name in ("env", "path", "output"))
            for path in (env_file, path_file, output_file):
                path.touch()
            env = {
                **os.environ,
                **defaults,
                "RUNNER_OS": "Linux",
                "RUNNER_ARCH": "X64",
                "RUNNER_TEMP": str(temp),
                "RUNNER_TOOL_CACHE": "/opt/actions-runner/_work/_tool",
                "AGENT_TOOLSDIRECTORY": "/opt/actions-runner/_work/_tool",
                "GITHUB_ENV": str(env_file),
                "GITHUB_PATH": str(path_file),
                "GITHUB_OUTPUT": str(output_file),
                "GITHUB_WORKSPACE": str(ROOT.parent),
                "INPUT_PYTHON-VERSION": setup["with"]["python-version"],
                "INPUT_CHECK-LATEST": "false",
                "INPUT_UPDATE-ENVIRONMENT": "true",
                "PYTHONPATH": str(ROOT),
            }
            installed = subprocess.run(
                ["node", str(action / "dist/setup/index.js")],
                env=env,
                capture_output=True,
                text=True,
                timeout=240,
            )
            # Matcher commands refer to container-only files. Keep diagnostics
            # and exit status, without forwarding commands to the outer runner.
            print((installed.stdout + installed.stderr).replace("::", ": :").replace("##[", "# #["))
            installed.check_returncode()
            env.update(action_environment(env_file))
            env["PATH"] = ":".join([*reversed(path_file.read_text().splitlines()), env["PATH"]])
            python = str(Path(env["pythonLocation"]) / "bin/python")
            clean = [
                "sudo",
                "-n",
                "env",
                "-i",
                "PATH=/usr/bin:/bin",
                python,
                "-I",
                "-c",
                "import sys; print(repr((sys.executable, sys.version, sys.prefix, sys.base_prefix)))",
            ]
            expected = subprocess.check_output(
                [python, "-c", clean[-1]], env=env, text=True
            ).strip()
            before = subprocess.run(clean, capture_output=True, text=True)
            subprocess.run(
                [python, "-c", runtime["runs"]["steps"][1]["run"]], env=env, check=True, timeout=60
            )
            after = subprocess.run(clean, check=True, timeout=30, capture_output=True, text=True)
            self.assertEqual(after.stdout.strip(), expected)
            # Use the actual composite action body and the context path from the
            # reusable clarification workflow, rather than a parallel fixture.
            workflow = yaml.safe_load((ROOT / ".github/workflows/clarify.yml").read_text())
            workspace = next(
                s
                for s in workflow["jobs"]["clarify"]["steps"]
                if s.get("uses") == "./kit/actions/agent-workspace"
            )
            (root / "fixture").mkdir()
            source, _, _, context = fixture(root / "fixture")
            context_path = Path(workspace["with"]["context"])
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text(json.dumps(context))
            composite = yaml.safe_load((ROOT / "actions/agent-workspace/action.yml").read_text())
            env.update(
                {
                    "NEXKIT_ROLE": workspace["with"]["role"],
                    "NEXKIT_CONTEXT": str(context_path),
                    "NEXKIT_SOURCE": str(source),
                }
            )
            try:
                subprocess.run(
                    [python, "-c", composite["runs"]["steps"][0]["run"]],
                    env=env,
                    check=True,
                    timeout=90,
                )
                self.assertEqual(
                    Path("/home/nexkit-agent/work/app.py").read_text(), "print('fixture')\n"
                )
                self.assertTrue((context_path.parent / "setup-directories.json").exists())
                self.assertTrue(Path("/home/nexkit-agent/output").is_dir())
            finally:
                subprocess.run(["pkill", "-KILL", "-u", "nexkit-agent"], capture_output=True)
                subprocess.run(["userdel", "nexkit-agent"], check=True)
                shutil.rmtree("/home/nexkit-agent")
                shutil.rmtree(context_path.parent)
            print(
                json.dumps(
                    {
                        "workflow_python": python,
                        "before_runtime_fix": before.returncode,
                        "before_matches_workflow_python": before.stdout.strip() == expected,
                        "sudo_snapshot_restore": True,
                        "model_calls": 0,
                    }
                )
            )
